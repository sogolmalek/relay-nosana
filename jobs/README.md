# Nosana vLLM Job Definition Template

`vllm.json.j2` is a Jinja2 template for a Nosana container job that runs a vLLM OpenAI-compatible server.

## Variables

| Variable | Description |
|---|---|
| `hf_id` | HuggingFace model ID (e.g. `meta-llama/Llama-3.1-8B-Instruct`) |
| `model_name` | Served model name (e.g. `llama-3.1-8b-instruct`) |
| `max_model_len` | Maximum context length in tokens |
| `quant` | Quantization type (`awq`, `gptq`, `bitsandbytes`) or empty string |
| `resources` | Optional list of S3 resource objects for cached model weights |

## Example rendered output

```json
{
  "ops": [{
    "type": "container",
    "image": "vllm/vllm-openai:latest",
    "gpu": true,
    "expose": 8000,
    "cmd": [
      "--model", "meta-llama/Llama-3.1-8B-Instruct",
      "--served-model-name", "llama-3.1-8b-instruct",
      "--max-model-len", "8192",
      "--port", "8000"
    ]
  }]
}
```

## Cached model resources

If the chosen market pre-caches model weights at an S3 URL, pass them via `resources` to skip
the HuggingFace download and reduce startup time significantly.

## Notes

- The job uses `vllm/vllm-openai:latest` — pin to a specific tag in production.
- `expose: 8000` makes the vLLM HTTP server reachable at `endpoints[0].url`.
- The relay POC detects guided-decoding support at startup and uses `response_format` with a JSON
  schema when available.
