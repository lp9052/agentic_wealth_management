"""
Regulatory RAG retrieval for the Proposer Agent.

Uses ChromaDB + Google embeddings to retrieve relevant regulation text
for context enrichment when generating proposals. This gives the proposer
awareness of regulations it might be violating, though the deterministic
auditor is the authoritative compliance check.
"""

import os
import json
from langchain_chroma import Chroma
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_core.documents import Document

from dotenv import load_dotenv
load_dotenv()

CHROMA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "chroma_db")
CHROMA_DIR = os.path.normpath(CHROMA_DIR)

# Cached vectorstore handle — building it instantiates the Google embeddings
# client and opens the persistent store, so we do it once and reuse it across
# every retrieve_regulations call on a revision cycle.
_vectorstore = None


def init_chroma():
    """Initialize (and cache) the ChromaDB vectorstore with Google embeddings."""
    global _vectorstore
    if _vectorstore is None:
        embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
        _vectorstore = Chroma(
            collection_name="regulations",
            embedding_function=embeddings,
            persist_directory=CHROMA_DIR,
        )
    return _vectorstore


def ingest_regulations():
    """Ingest regulations.json into ChromaDB for RAG retrieval."""
    reg_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "regulations.json")
    reg_path = os.path.normpath(reg_path)

    with open(reg_path, "r") as f:
        data = json.load(f)

    docs = []
    for item in data:
        metadata = item["metadata"]
        header = (
            f"Rule: {metadata['rule']}\n"
            f"GraphRAG ID: {item['id']}\n"
            f"Related: {', '.join(metadata['related'])}\n"
            f"Tags: {', '.join(metadata['tags'])}\n\n"
        )
        content = header + item["text"]

        flat_meta = {
            "id": item["id"],
            "rule": metadata["rule"],
            "related": ", ".join(metadata["related"]),
            "tags": ", ".join(metadata["tags"]),
        }

        doc = Document(page_content=content, metadata=flat_meta)
        docs.append(doc)

    vectorstore = init_chroma()
    vectorstore.add_documents(docs)
    print(f"Ingested {len(docs)} regulations into ChromaDB.")


def retrieve_regulations(query: str, k: int = 2) -> list[str]:
    """Retrieve relevant regulation text for a given query."""
    vectorstore = init_chroma()
    results = vectorstore.similarity_search(query, k=k)
    return [r.page_content for r in results]


if __name__ == "__main__":
    ingest_regulations()
