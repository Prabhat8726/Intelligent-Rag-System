"""Human review queue (Module 26): one open task per document listing everything to check.

What the machine found is recorded on the document (`review_reasons`) and in detail on the
task. Whether a person still has to look is the task: a document is REVIEW_REQUIRED exactly
while it has an open task. A finding a reviewer resolved does not reopen a task for the same
document version; a new finding does.
"""
