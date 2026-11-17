"""Knowledge sources. Add a source by adding a loader to `default_loaders()`."""

from langchain_core.documents import Document

from pipeline_incident_copilot.config import DATABRICKS_DIR, EXTERNAL_DATA_DIR, PROJECT_ROOT, RUNTIME_DIR
from pipeline_incident_copilot.rag.ingestion.loaders.base import DocumentLoader
from pipeline_incident_copilot.rag.ingestion.loaders.databricks_error_reference_loader import (
    DatabricksErrorReferenceLoader,
    known_classes,
)
from pipeline_incident_copilot.rag.ingestion.loaders.error_catalog_loader import ErrorCatalogLoader
from pipeline_incident_copilot.rag.ingestion.loaders.markdown_document_loader import MarkdownDocumentLoader
from pipeline_incident_copilot.rag.ingestion.loaders.notebook_loader import NotebookLoader
from pipeline_incident_copilot.rag.ingestion.loaders.support_ticket_loader import SupportTicketLoader
from pipeline_incident_copilot.rag.ingestion.loaders.uploaded_document_loader import UploadedDocumentLoader

ERROR_CATALOG_DIR = EXTERNAL_DATA_DIR / "error_catalogs"
PIPELINE_NOTEBOOKS_DIR = DATABRICKS_DIR / "notebooks"
KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"


def default_loaders() -> list[DocumentLoader]:
    spark_catalog = ERROR_CATALOG_DIR / "spark_error_conditions.json"
    delta_catalog = ERROR_CATALOG_DIR / "delta_error_classes.json"
    return [
        ErrorCatalogLoader(spark_catalog, "spark"),
        ErrorCatalogLoader(delta_catalog, "delta"),
        DatabricksErrorReferenceLoader(
            ERROR_CATALOG_DIR / "databricks_error_classes.html",
            exclude_classes=known_classes(spark_catalog, delta_catalog),
        ),
        NotebookLoader(PIPELINE_NOTEBOOKS_DIR),
        MarkdownDocumentLoader(KNOWLEDGE_DIR / "architecture", "architecture", parent_per_section=True),
        MarkdownDocumentLoader(KNOWLEDGE_DIR / "runbooks", "runbook"),
        SupportTicketLoader(KNOWLEDGE_DIR / "tickets"),
        UploadedDocumentLoader(RUNTIME_DIR / "knowledge_uploads"),  # company documents from the console
    ]


def load_all(loaders: list[DocumentLoader] | None = None) -> list[Document]:
    return [doc for loader in loaders or default_loaders() for doc in loader.load()]
