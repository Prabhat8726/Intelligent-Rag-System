import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type SubmitEvent } from "react";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { DocumentSummary, Sensitivity } from "../lib/types";

const ACCEPT = ".pdf,.png,.jpg,.jpeg,.tif,.tiff";
const SENSITIVITIES: Sensitivity[] = ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"];

export function UploadForm() {
  const { token } = useAuth();
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [sensitivity, setSensitivity] = useState<Sensitivity>("INTERNAL");

  const upload = useMutation({
    mutationFn: (selected: File) => {
      const form = new FormData();
      form.append("file", selected);
      form.append("sensitivity", sensitivity);
      return apiRequest<DocumentSummary>("/api/v1/documents", { method: "POST", body: form, token });
    },
    onSuccess: () => {
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
      void queryClient.invalidateQueries({ queryKey: ["documents"] });
    },
  });

  function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (file) upload.mutate(file);
  }

  const error =
    upload.error instanceof ApiError
      ? (upload.error.problem?.detail ?? upload.error.message)
      : upload.error
        ? "Upload failed. Please try again."
        : null;

  return (
    <form
      onSubmit={handleSubmit}
      aria-label="Upload document"
      className="flex flex-wrap items-end gap-4 rounded-xl border border-slate-200 bg-white p-5"
    >
      <div className="min-w-64 flex-1">
        <label htmlFor="document-file" className="block text-sm font-medium text-slate-700">
          Document (PDF, PNG, JPEG, TIFF · max 25 MB)
        </label>
        <input
          ref={inputRef}
          id="document-file"
          type="file"
          accept={ACCEPT}
          onChange={(event) => {
            setFile(event.target.files?.[0] ?? null);
            upload.reset();
          }}
          className="mt-1 block w-full text-sm text-slate-700 file:mr-3 file:rounded-md file:border-0 file:bg-slate-100 file:px-3 file:py-1.5 file:text-sm"
        />
      </div>
      <div>
        <label htmlFor="document-sensitivity" className="block text-sm font-medium text-slate-700">
          Sensitivity
        </label>
        <select
          id="document-sensitivity"
          value={sensitivity}
          onChange={(event) => {
            setSensitivity(event.target.value as Sensitivity);
          }}
          className="mt-1 rounded-md border border-slate-300 px-3 py-1.5 text-sm"
        >
          {SENSITIVITIES.map((value) => (
            <option key={value} value={value}>
              {value.charAt(0) + value.slice(1).toLowerCase()}
            </option>
          ))}
        </select>
      </div>
      <button
        type="submit"
        disabled={!file || upload.isPending}
        className="rounded-md bg-blue-900 px-4 py-2 text-sm font-medium text-white hover:bg-blue-800 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {upload.isPending ? "Uploading…" : "Upload"}
      </button>
      {error && (
        <p role="alert" className="w-full rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}
      {upload.isSuccess && (
        <p role="status" className="w-full text-sm text-emerald-700">
          Uploaded “{upload.data.display_filename}” — queued for processing.
          {upload.data.duplicate_of_id && " An identical file already exists; it has been flagged as a duplicate."}
        </p>
      )}
    </form>
  );
}
