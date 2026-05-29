from dataclasses import dataclass

from config.settings import get_settings
from document_pipeline.parsers import ParsedChunkRaw

settings = get_settings()


@dataclass
class Chunk:
    text: str
    page_start: int
    page_end: int
    section: str | None
    chunk_type: str  # "text" | "table"
    chunk_index: int


def chunk_document(raw_chunks: list[ParsedChunkRaw]) -> list[Chunk]:
    """
    Tables: one complete chunk, never split.
    Text: recursive split on paragraph → sentence → word boundaries with overlap.
    """
    result: list[Chunk] = []
    global_index = 0

    for raw in raw_chunks:
        if raw.chunk_type == "table":
            result.append(Chunk(
                text=raw.text,
                page_start=raw.page_start,
                page_end=raw.page_end,
                section=raw.section,
                chunk_type="table",
                chunk_index=global_index,
            ))
            global_index += 1
        else:
            splits = _recursive_split(raw.text, settings.KB_CHUNK_SIZE, settings.KB_CHUNK_OVERLAP)
            for split_text in splits:
                if split_text.strip():
                    result.append(Chunk(
                        text=split_text.strip(),
                        page_start=raw.page_start,
                        page_end=raw.page_end,
                        section=raw.section,
                        chunk_type="text",
                        chunk_index=global_index,
                    ))
                    global_index += 1

    return result


def _recursive_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    separators = ["\n\n", "\n", ". ", " ", ""]
    return _split_recursive(text, separators, chunk_size, overlap)


def _split_recursive(text: str, separators: list[str], chunk_size: int, overlap: int) -> list[str]:
    if len(text) <= chunk_size:
        return [text]

    sep = separators[0] if separators else ""
    parts = text.split(sep) if sep else list(text)

    chunks: list[str] = []
    current = ""

    for part in parts:
        candidate = current + (sep if current else "") + part
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(part) > chunk_size and len(separators) > 1:
                sub = _split_recursive(part, separators[1:], chunk_size, overlap)
                chunks.extend(sub[:-1])
                current = sub[-1] if sub else ""
            else:
                current = part

    if current:
        chunks.append(current)

    if overlap > 0 and len(chunks) > 1:
        overlapped = [chunks[0]]
        for i in range(1, len(chunks)):
            tail = chunks[i - 1][-overlap:]
            overlapped.append(tail + " " + chunks[i])
        return overlapped

    return chunks
