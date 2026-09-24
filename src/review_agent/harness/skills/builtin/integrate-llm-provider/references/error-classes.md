# Error classification — integrate-llm-provider

| Class | Typical cause | Caller expectation |
| --- | --- | --- |
| `timeout` | Network/read deadline exceeded | Retry with backoff or fail the step; do not invent usage |
| `invalid_response` | Malformed JSON / missing required fields | Fail closed; log excerpt without secrets |
| `auth` | Missing/invalid API key | Fail; do not retry blindly |
| `other` | HTTP 5xx, unknown provider codes | Record status; optional limited retry |

Unknown usage must not be displayed as zero.
