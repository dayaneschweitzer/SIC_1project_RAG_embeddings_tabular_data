"""Encoders fixados usados pela D1-v3 e pela D2 de paridade."""
from __future__ import annotations

from pathlib import Path

from .contracts import file_hash
from .encoder_specs import spec_for


ENCODER_IDS = {"minilm": "E1_MINILM", "taberta": "E2_TABERTA"}
THRESHOLDS = {"E1_MINILM": 0.14, "E2_TABERTA": 0.96}
DECISION_CONDITIONS = {"E1_MINILM": "D1-A-v3", "E2_TABERTA": "D1-B-v3"}


def model_directory(encoder_id, local_model=None):
    spec = spec_for(encoder_id)
    if local_model is not None:
        path = Path(local_model).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError("--model deve apontar para uma pasta local do encoder fixado")
    else:
        from huggingface_hub import snapshot_download

        patterns = [f"{spec.variant}/*"] if spec.variant else None
        root = Path(snapshot_download(spec.repo_id, revision=spec.revision, allow_patterns=patterns))
        path = root / spec.variant if spec.variant else root
    for name, expected in ENCODER_FILE_HASHES[encoder_id].items():
        if file_hash(path / name) != expected:
            raise ValueError(f"ENCODER_FILE_IDENTITY:{encoder_id}:{name}")
    return path


def load_encoders(encoder_id, local_model=None):
    """Carrega o encoder ativo e os dois tokenizers do orçamento tabular comum."""
    from sentence_transformers import SentenceTransformer
    from transformers import AutoTokenizer

    path = model_directory(encoder_id, local_model)
    tokenizers = {}
    for identity in ENCODER_IDS.values():
        spec = spec_for(identity)
        if identity == encoder_id:
            tokenizers[identity] = AutoTokenizer.from_pretrained(
                str(path), local_files_only=True, trust_remote_code=False,
            )
        else:
            tokenizers[identity] = AutoTokenizer.from_pretrained(
                spec.repo_id, revision=spec.revision, subfolder=spec.variant or "",
                trust_remote_code=False,
            )
    encoder = SentenceTransformer(
        str(path), device="cpu", local_files_only=True, trust_remote_code=False,
    )
    encoder.max_seq_length = 256
    encoder.eval()
    encoder.requires_grad_(False)
    if encoder.get_sentence_embedding_dimension() != spec_for(encoder_id).dimension:
        raise ValueError("ENCODER_DIMENSION")
    return encoder, tokenizers


ENCODER_FILE_HASHES = {'E1_MINILM': {'1_Pooling/config.json': '4be450dde3b0273bb9787637cfbd28fe04a7ba6ab9d36ac48e92b11e350ffc23', 'README.md': 'dcd602d2fd35c203a247304a06fec6654a12f7941b739f9221a064fe8dc3b7f0', 'config.json': '953f9c0d463486b10a6871cc2fd59f223b2c70184f49815e7efbcab5d8908b41', 'config_sentence_transformers.json': '061ca9d39661d6c6d6de5ba27f79a1cd5770ea247f8d46412a68a498dc5ac9f3', 'model.safetensors': '53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db', 'modules.json': '84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf', 'sentence_bert_config.json': 'fc1993fde0a95c24ec6c022539d41cf6e2f7c9721e5415d6fb6897472a9cd4b7', 'special_tokens_map.json': '303df45a03609e4ead04bc3dc1536d0ab19b5358db685b6f3da123d05ec200e3', 'tokenizer.json': 'be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037', 'tokenizer_config.json': 'acb92769e8195aabd29b7b2137a9e6d6e25c476a4f15aa4355c233426c61576b', 'vocab.txt': '07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3'}, 'E2_TABERTA': {'1_Pooling/config.json': '1c8bc7bd750c5c20d8707f1c5c578f5d69bb3d1d5ebcf4b2fde5128de154ec1c', 'README.md': 'ec7a2110bcec811be55813b5e9d7bcf6b3b4a6f62772f0d343de8cfb664cb958', 'config.json': 'af4ca72f5e62e1481fef64bd7a24d5d756bacae6912d64666f082158f57b94fa', 'config_sentence_transformers.json': '7d6a5c6de857fe02c3933a9e999c12f4c82d229f7fd509931b5fc996888bf4d8', 'model.safetensors': '13ddafe0cb40ad24bb5f5bad92bd723055d97eddc48972593aa544a419b61f82', 'modules.json': '8f4b264b80206c830bebbdcae377e137925650a433b689343a63bdc9b3145460', 'sentence_bert_config.json': 'fc1993fde0a95c24ec6c022539d41cf6e2f7c9721e5415d6fb6897472a9cd4b7', 'special_tokens_map.json': 'b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3', 'tokenizer.json': 'd241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66', 'tokenizer_config.json': 'b10367478f3ef5186ffa8f421664e9a2313f06d19764876ff27014ec880dd06a', 'vocab.txt': '07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3'}}
