from __future__ import annotations

DEFAULT_SENTENCE_TRANSFORMER_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class EmbeddingProviderError(RuntimeError):
    pass


class SentenceTransformerEmbeddingProvider:
    def __init__(self, model_name: str = DEFAULT_SENTENCE_TRANSFORMER_MODEL) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise EmbeddingProviderError(
                "sentence-transformers is not installed. Install requirements.txt."
            ) from error

        try:
            self.model = SentenceTransformer(model_name)
        except Exception as error:
            raise EmbeddingProviderError(
                f"Could not load sentence-transformers model {model_name}: {error}"
            ) from error

        self.model_name = model_name
        self.provider_name = "sentence-transformers"
        self.dimension = self._resolve_embedding_dimension()

    def encode(self, texts: list[str]) -> list[list[float]]:
        embeddings = self.model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embeddings.astype("float32").tolist()

    def _resolve_embedding_dimension(self) -> int:
        if hasattr(self.model, "get_embedding_dimension"):
            return int(self.model.get_embedding_dimension())

        return int(self.model.get_sentence_embedding_dimension())


def create_embedding_provider(
    provider: str = "sentence-transformers",
    model_name: str = DEFAULT_SENTENCE_TRANSFORMER_MODEL,
) -> SentenceTransformerEmbeddingProvider:
    if provider != "sentence-transformers":
        raise ValueError(f"Unsupported embedding provider: {provider}")
    return SentenceTransformerEmbeddingProvider(model_name=model_name)


def create_embedding_provider_from_config(config: dict):
    return create_embedding_provider(
        provider=config.get("embedding_provider"),
        model_name=config.get("embedding_model") or DEFAULT_SENTENCE_TRANSFORMER_MODEL,
    )
