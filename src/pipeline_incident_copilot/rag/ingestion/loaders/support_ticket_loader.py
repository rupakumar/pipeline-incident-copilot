"""Support-ticket history (markdown with front matter) → one Document per ticket."""

from pathlib import Path

from pipeline_incident_copilot.rag.ingestion.loaders.markdown_document_loader import MarkdownDocumentLoader


class SupportTicketLoader(MarkdownDocumentLoader):
    metadata_keys = ("ticket_id", "status", "severity", "opened_at", "resolved_at", "failure_class", "source_run_id")

    def __init__(self, directory: Path):
        super().__init__(directory, source_type="ticket")
