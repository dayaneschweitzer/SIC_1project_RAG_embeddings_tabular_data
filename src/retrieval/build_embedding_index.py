from __future__ import annotations

from datetime import datetime
from pathlib import Path

try:
    from .embedding_provider import (
        DEFAULT_SENTENCE_TRANSFORMER_MODEL,
        create_embedding_provider,
    )
    from .vector_index import (
        INDEX_STRATEGY,
        SIMILARITY_METRIC,
        infer_basename_from_semantic_blocks_path,
        load_semantic_blocks,
        metadata_from_semantic_blocks,
        save_index_artifacts,
    )
except ImportError:
    from embedding_provider import (
        DEFAULT_SENTENCE_TRANSFORMER_MODEL,
        create_embedding_provider,
    )
    from vector_index import (
        INDEX_STRATEGY,
        SIMILARITY_METRIC,
        infer_basename_from_semantic_blocks_path,
        load_semantic_blocks,
        metadata_from_semantic_blocks,
        save_index_artifacts,
    )


def build_embedding_index(
    semantic_blocks_path: str,
    output_dir: str,
    provider_name: str = "sentence-transformers",
    model_name: str = DEFAULT_SENTENCE_TRANSFORMER_MODEL,
    basename: str | None = None,
) -> dict:
    semantic_blocks = load_semantic_blocks(semantic_blocks_path)
    metadata = metadata_from_semantic_blocks(semantic_blocks)

    if not metadata:
        raise ValueError("semantic_blocks does not contain recoverable blocks")

    texts = [entry["retrieval_text"] for entry in metadata]
    provider = create_embedding_provider(
        provider=provider_name,
        model_name=model_name,
    )
    embeddings = provider.encode(texts)
    embedding_dimension = len(embeddings[0]) if embeddings else 0

    basename = basename or infer_basename_from_semantic_blocks_path(semantic_blocks_path)
    config = {
        "embedding_model": provider.model_name,
        "embedding_provider": provider.provider_name,
        "requested_embedding_model": model_name,
        "embedding_dimension": embedding_dimension,
        "similarity_metric": SIMILARITY_METRIC,
        "total_vectors": len(embeddings),
        "source_semantic_blocks": str(Path(semantic_blocks_path)),
        "created_at": datetime.now().isoformat(),
        "index_strategy": INDEX_STRATEGY,
    }

    paths = save_index_artifacts(
        embeddings=embeddings,
        metadata=metadata,
        config=config,
        output_dir=output_dir,
        basename=basename,
    )

    print("\nIndice de embeddings gerado:")
    print(f"Embeddings: {paths['embeddings']}")
    print(f"Metadados: {paths['metadata']}")
    print(f"Configuracao: {paths['config']}")
    print(f"Modelo: {config['embedding_model']}")
    print(f"Dimensao: {config['embedding_dimension']}")
    print(f"Total de vetores: {config['total_vectors']}")

    return {
        "paths": paths,
        "config": config,
        "metadata": metadata,
    }
