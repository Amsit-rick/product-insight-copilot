from fastapi.testclient import TestClient

from local_models.app import LocalModelRuntime, create_app


class FakeRuntime:
    embedding_model = "test-embedding"
    reranker_model = "test-reranker"

    def __init__(self):
        self.loaded = False

    def load(self):
        self.loaded = True

    def embed(self, texts):
        return [[float(index), 1.0, 0.0] for index, _text in enumerate(texts)]

    def rerank(self, query, documents):
        return [0.1 if "无关" in item else 0.9 for item in documents]

    def description(self):
        return {
            "loaded": self.loaded,
            "embedding_model": self.embedding_model,
            "reranker_model": self.reranker_model,
            "device": "test",
            "model_threads": 1,
            "inference_policy": "fake",
        }


def test_local_model_api_requires_auth_and_matches_contract():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime, api_key="secret")) as client:
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert health["inference_policy"] == "fake"
        assert client.post(
            "/v1/embeddings", json={"model": "test-embedding", "input": ["a"]}
        ).status_code == 401
        headers = {"Authorization": "Bearer secret"}
        embedding = client.post(
            "/v1/embeddings",
            headers=headers,
            json={"model": "test-embedding", "input": ["a", "b"]},
        )
        assert embedding.status_code == 200
        assert [row["index"] for row in embedding.json()["data"]] == [0, 1]
        assert all(len(row["embedding"]) == 3 for row in embedding.json()["data"])

        reranked = client.post(
            "/rerank",
            headers=headers,
            json={"query": "退款", "documents": ["无关物流", "退款规则"], "top_n": 2},
        )
        assert reranked.status_code == 200
        assert [row["index"] for row in reranked.json()["results"]] == [1, 0]


def test_local_model_api_rejects_wrong_model_and_oversized_batch():
    with TestClient(create_app(FakeRuntime(), api_key="secret")) as client:
        headers = {"Authorization": "Bearer secret"}
        wrong = client.post(
            "/v1/embeddings", headers=headers, json={"model": "wrong", "input": ["a"]}
        )
        assert wrong.status_code == 400
        oversized = client.post(
            "/v1/embeddings",
            headers=headers,
            json={"model": "test-embedding", "input": ["a"] * 129},
        )
        assert oversized.status_code == 422


def test_local_runtime_describes_serial_inference_policy_without_loading_models():
    runtime = LocalModelRuntime(
        embedding_model="test-embedding",
        reranker_model="test-reranker",
        threads=2,
    )

    description = runtime.description()

    assert description["loaded"] is False
    assert description["model_threads"] == 2
    assert description["inference_policy"] == "shared_serial"
