import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type SubmitEvent, useRef, useState } from "react";

import { useAuth } from "../auth/useAuth";
import { ApiError, apiRequest } from "../lib/api";
import type { KnowledgeCategory, KnowledgeDocument, Sensitivity } from "../lib/types";
import { CATEGORIES, CATEGORY_LABELS } from "./format";

const ACCEPT = ".md,.markdown,.txt,.pdf,.png,.jpg,.jpeg,.tif,.tiff";
const SENSITIVITIES: Sensitivity[] = ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"];

/** Fields left empty come from the file's front matter (Markdown/text) or defaults. */
export function KnowledgeUploadForm() {
  const { token, user } = useAuth();
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [category, setCategory] = useState<KnowledgeCategory | "">("");
  const [title, setTitle] = useState("");
  const [documentKey, setDocumentKey] = useState("");
  const [version, setVersion] = useState("");
  const [sensitivity, setSensitivity] = useState<Sensitivity | "">("");
  const [effectiveFrom, setEffectiveFrom] = useState("");
  const [departmentOnly, setDepartmentOnly] = useState(false);

  const upload = useMutation({
    mutationFn: (selected: File) => {
      const form = new FormData();
      form.append("file", selected);
      const fields: Record<string, string> = {
        category,
        title: title.trim(),
        document_key: documentKey.trim(),
        version_label: version.trim(),
        sensitivity,
        effective_from: effectiveFrom,
      };
      for (const [name, value] of Object.entries(fields)) {
        if (value) form.append(name, value);
      }
      if (departmentOnly && user?.department) form.append("department_id", user.department.id);
      return apiRequest<KnowledgeDocument>("/api/v1/knowledge/documents", { method: "POST", body: form, token });
    },
    onSuccess: () => {
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
      void queryClient.invalidateQueries({ queryKey: ["knowledge"] });
    },
  });

  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (file) upload.mutate(file);
  };
  const error =
    upload.error instanceof ApiError
      ? (upload.error.problem?.detail ?? upload.error.message)
      : upload.error
        ? "Upload failed. Please try again."
        : null;
  const input = "mt-1 rounded-md border border-slate-300 px-2 py-1 text-sm";

  return (
    <form
      onSubmit={submit}
      aria-label="Add knowledge document"
      className="space-y-3 rounded-xl border border-slate-200 bg-white p-5"
    >
      <div>
        <label htmlFor="knowledge-file" className="block text-sm font-medium text-slate-700">
          Document (Markdown, text, PDF or image) — a file with the same document key becomes its new version
        </label>
        <input
          ref={inputRef}
          id="knowledge-file"
          type="file"
          accept={ACCEPT}
          onChange={(event) => {
            setFile(event.target.files?.[0] ?? null);
            upload.reset();
          }}
          className="mt-1 block w-full text-sm text-slate-700 file:mr-3 file:rounded-md file:border-0 file:bg-slate-100 file:px-3 file:py-1.5 file:text-sm"
        />
      </div>
      <div className="flex flex-wrap items-end gap-3 text-xs">
        <label>
          <span className="block text-slate-500">Category</span>
          <select
            value={category}
            onChange={(event) => {
              setCategory(event.target.value as KnowledgeCategory | "");
            }}
            className={input}
          >
            <option value="">From the file</option>
            {CATEGORIES.map((value) => (
              <option key={value} value={value}>
                {CATEGORY_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span className="block text-slate-500">Title</span>
          <input
            value={title}
            maxLength={300}
            onChange={(event) => {
              setTitle(event.target.value);
            }}
            className={input}
          />
        </label>
        <label>
          <span className="block text-slate-500">Document key</span>
          <input
            value={documentKey}
            maxLength={100}
            placeholder="procurement-policy"
            onChange={(event) => {
              setDocumentKey(event.target.value);
            }}
            className={input}
          />
        </label>
        <label>
          <span className="block text-slate-500">Version</span>
          <input
            value={version}
            maxLength={50}
            onChange={(event) => {
              setVersion(event.target.value);
            }}
            className={`${input} w-24`}
          />
        </label>
        <label>
          <span className="block text-slate-500">Effective from</span>
          <input
            type="date"
            value={effectiveFrom}
            onChange={(event) => {
              setEffectiveFrom(event.target.value);
            }}
            className={input}
          />
        </label>
        <label>
          <span className="block text-slate-500">Sensitivity</span>
          <select
            value={sensitivity}
            onChange={(event) => {
              setSensitivity(event.target.value as Sensitivity | "");
            }}
            className={input}
          >
            <option value="">From the file</option>
            {SENSITIVITIES.map((value) => (
              <option key={value} value={value}>
                {value.charAt(0) + value.slice(1).toLowerCase()}
              </option>
            ))}
          </select>
        </label>
        {user?.department && (
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={departmentOnly}
              onChange={(event) => {
                setDepartmentOnly(event.target.checked);
              }}
            />
            Only {user.department.name}
          </label>
        )}
        <button
          type="submit"
          disabled={!file || upload.isPending}
          className="rounded-md bg-blue-900 px-4 py-2 text-sm font-medium text-white hover:bg-blue-800 disabled:opacity-50"
        >
          {upload.isPending ? "Uploading…" : "Add"}
        </button>
      </div>
      {error && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}
      {upload.isSuccess && (
        <p role="status" className="text-sm text-emerald-700">
          Added “{upload.data.title}” — it is searchable once processed.
        </p>
      )}
    </form>
  );
}
