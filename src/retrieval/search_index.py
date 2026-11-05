from __future__ import annotations


try:
    from .embedding_provider import create_embedding_provider_from_config
    from .vector_index import VectorIndex, infer_single_basename, load_index_artifacts
except ImportError:
    from embedding_provider import create_embedding_provider_from_config
    from vector_index import VectorIndex, infer_single_basename, load_index_artifacts


def search_index(
    query: str,
    top_k: int = 5,
    index_dir: str = "data/index",
    basename: str | None = None,
) -> list[dict]:
    basename = basename or infer_single_basename(index_dir)
    embeddings, metadata, config = load_index_artifacts(index_dir=index_dir, basename=basename)

    provider = create_embedding_provider_from_config(config)
    query_embedding = provider.encode([query])[0]
    index = VectorIndex(
        embeddings=embeddings,
        metadata=metadata,
        similarity_metric=config.get("similarity_metric", "cosine"),
    )

    return index.search(query_embedding=query_embedding, top_k=top_k)
