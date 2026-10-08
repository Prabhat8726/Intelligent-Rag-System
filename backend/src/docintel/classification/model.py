"""Local document-type classifier (stage 1): TF-IDF (word + character n-grams) and logistic
regression, probability-calibrated with Platt scaling (sigmoid, 3-fold).

Free, private (nothing leaves the worker) and fast. The model is trained when the worker starts
from the synthetic corpus plus human corrections (ADR-022), so there is no pickled model file to
trust. Every prediction carries the model fingerprint (training data + library versions).
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import sklearn
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import FeatureUnion, Pipeline

from docintel.classification.corpus import LabelledText
from docintel.db.models import DocumentType

MODEL_VERSION = "tfidf-lr-v1"
# Classification reads the start of a document: titles, parties and headers carry the signal,
# long tails (100 line items) only add noise and cost.
MAX_CHARS = 6000
MIN_TEXT_CHARS = 20
_DIGITS = re.compile(r"\d")
_SPACES = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Lower-case, digits collapsed to 0 (amounts and dates are not type evidence), spaces."""
    return _SPACES.sub(" ", _DIGITS.sub("0", text[:MAX_CHARS].lower())).strip()


def has_enough_text(text: str) -> bool:
    return len(normalize_text(text).replace(" ", "")) >= MIN_TEXT_CHARS


@dataclass(frozen=True, slots=True)
class LocalPrediction:
    probabilities: dict[DocumentType, float]

    def ranked(self) -> list[tuple[DocumentType, float]]:
        return sorted(self.probabilities.items(), key=lambda item: item[1], reverse=True)

    @property
    def label(self) -> DocumentType:
        return self.ranked()[0][0]

    @property
    def confidence(self) -> float:
        return self.ranked()[0][1]

    def top(self, count: int) -> list[tuple[DocumentType, float]]:
        return self.ranked()[:count]


def _fingerprint(samples: Sequence[LabelledText]) -> str:
    digest = hashlib.sha256(f"{MODEL_VERSION}|sklearn {sklearn.__version__}".encode())
    for sample in sorted(samples, key=lambda s: (s.label.value, s.text)):
        digest.update(sample.label.value.encode())
        digest.update(hashlib.sha256(sample.text.encode()).digest())
    return f"{MODEL_VERSION}:{digest.hexdigest()[:16]}"


class LocalClassifier:
    def __init__(
        self,
        pipeline: Pipeline,
        labels: list[DocumentType],
        fingerprint: str,
        size: int,
        seconds: float,
    ) -> None:
        self._pipeline = pipeline
        self._labels = labels
        self.fingerprint = fingerprint
        self.training_size = size
        self.training_seconds = seconds

    @classmethod
    def train(cls, samples: Sequence[LabelledText]) -> LocalClassifier:
        labels = sorted({sample.label for sample in samples}, key=lambda label: label.value)
        if len(labels) < 2:
            msg = "training needs at least two document types"
            raise ValueError(msg)
        started = time.perf_counter()
        features = FeatureUnion(
            [
                (
                    "words",
                    TfidfVectorizer(
                        ngram_range=(1, 2), min_df=2, max_features=40_000, sublinear_tf=True
                    ),
                ),
                (
                    "chars",
                    TfidfVectorizer(
                        analyzer="char_wb",
                        ngram_range=(3, 5),
                        min_df=2,
                        max_features=60_000,
                        sublinear_tf=True,
                    ),
                ),
            ]
        )
        model = CalibratedClassifierCV(
            LogisticRegression(C=4.0, max_iter=2000), method="sigmoid", cv=3
        )
        pipeline = Pipeline([("features", features), ("model", model)])
        pipeline.fit(
            [normalize_text(sample.text) for sample in samples],
            [sample.label.value for sample in samples],
        )
        return cls(
            pipeline,
            labels,
            _fingerprint(samples),
            len(samples),
            time.perf_counter() - started,
        )

    @property
    def labels(self) -> list[DocumentType]:
        return list(self._labels)

    def predict(self, text: str) -> LocalPrediction:
        return self.predict_many([text])[0]

    def predict_many(self, texts: Sequence[str]) -> list[LocalPrediction]:
        matrix = np.asarray(self._pipeline.predict_proba([normalize_text(t) for t in texts]))
        classes = [DocumentType(value) for value in self._pipeline.classes_]
        return [
            LocalPrediction({label: float(p) for label, p in zip(classes, row, strict=True)})
            for row in matrix
        ]
