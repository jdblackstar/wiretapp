# Wiretapp

Easy-to-integrate Telemetry and Product Metrics for apps that use the OpenAI API

Wiretapp provides an open-source Python SDK and collector service to capture telemetry from OpenAI API calls. Integrate the SDK into your app, run the core Wiretapp services with Docker, and start collecting data on API usage, token counts, and performance. Advanced analytics and dashboards (e.g., for DAU, retention) can be built by extending the pipeline with additional components.

## Why Wiretapp?

Instant visibility – Know exactly how users and models behave without sending your data to a third‑party SaaS.

Privacy first – Hashes user identifiers, never logs raw prompts by default.

Full control – Everything runs in your cloud, on your laptop, or inside a CI job.

Batteries included – SDK wrappers and an ingestion API to get you started. (Future plans include more extensive data processing and dashboarding capabilities).

Quick start

```
# 1. Get the code
 git clone https://github.com/jdblackstar/wiretapp.git
 cd wiretapp

# 2. Launch the Kafka and Collector services
 docker compose up -d
```

In your Python app:

```
import openai
import wiretapp

wiretapp.monitor(openai, app_name="my_chat_app")

response = openai.ChatCompletion.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "hello"}]
)
```

Collected data will be sent to your Wiretapp collector. Further setup of the data pipeline (including Spark, dbt) and dashboarding tools (like Superset) is required to visualize advanced metrics.

## Using the Wiretapp SDK

This section explains how to integrate and use the Wiretapp Python SDK in your application to automatically capture and send telemetry data from OpenAI API calls.

**1. Initialization**

To begin, import the `wiretapp` library into your Python application alongside `openai`. The key step is to call `wiretapp.monitor()` early in your application's lifecycle—before any OpenAI API calls are made.

```python
import openai import OpenAI
import wiretapp

client = OpenAI()

# Initialize Wiretapp
# This should be done once, before any OpenAI API calls.
wiretapp.monitor(
    openai_module=client,  # Pass your imported openai library instance
    app_name="your_application_name"  # A descriptive name for your application
)

# Your application can now use the OpenAI API as usual.
# Wiretapp will automatically monitor the calls.
# For example:
# response = client.ChatCompletion.create(
#     model="gpt-4o",
#     messages=[{"role": "user", "content": "Tell me a joke."}]
# )
```

*   **`openai_module` (Required):** You must pass the `openai` library object that your application imports and uses. Wiretapp uses this to identify and monitor the API calls.
*   **`app_name` (Recommended):** Providing a unique `app_name` helps you distinguish telemetry data from different applications in your Wiretapp dashboards.

**2. How It Works**

The `wiretapp.monitor()` function intelligently modifies (also known as "monkey-patches") the `openai` library instance you provide. Once monitored, whenever your application calls functions like `openai.ChatCompletion.create()`, Wiretapp automatically intercepts the call to record key details about the request and the response. This information is then sent as telemetry to your Wiretapp collector service, typically over HTTP. This process is designed to be seamless and operate in the background.

**3. What Data is Sent?**

Wiretapp is built with privacy as a core principle:

*   **User and Session Identifiers:** To help analyze usage, Wiretapp can associate telemetry with user and session identifiers. These are automatically converted into non-reversible, scrambled codes (using SHA-256 hashing) by the SDK *before* any data is transmitted. This ensures that raw user IDs are not sent.
*   **Prompts and Completions Text:** By default, the actual text content of user prompts and the AI model's responses are **not** recorded or sent. This is a key privacy-preserving feature. If you need to capture this content, you can enable it via configuration (see below).
*   **API Call Metadata:** Wiretapp records valuable metadata about each API call, including:
    *   The AI model used (e.g., `gpt-4o`).
    *   Token usage (prompt tokens, completion tokens, total tokens).
    *   Latency (how long the API call took).
    *   Error codes and messages, if any occur.
    *   The `app_name` you provided.

**4. Configuration**

The Wiretapp SDK can be configured through parameters to the `monitor()` function and environment variables:

*   **`app_name` (parameter):** As shown in the initialization example, pass this directly to `wiretapp.monitor()`.
*   **`WIRETAPP_ENDPOINT` (Environment Variable):** This crucial variable tells the SDK the network address of your Wiretapp collector service's `/events` endpoint. Ensure this is set correctly in your application's environment. For example: `WIRETAPP_ENDPOINT=http://localhost:8000/events`. Refer to the main `## Configuration` section of this README for more on setting up your `.env` file.
*   **`WIRETAPP_INCLUDE_CONTENT=1` (Environment Variable):** To record the full text of prompts and completions, set this environment variable to `1`. Before enabling this, carefully consider the privacy implications and ensure compliance with any applicable regulations.
    *   Example: `export WIRETAPP_INCLUDE_CONTENT=1` (in your shell) or set it in your application's environment.
*   **Other Configurations:** The SDK might support additional environment variables for finer control (e.g., connection timeouts, custom identifier hashing). Please refer to the SDK's specific documentation or source code if you need more advanced customization.

**5. Supported OpenAI Functionality**

After initialization with `wiretapp.monitor()`, the SDK automatically tracks common OpenAI API calls, such as `openai.ChatCompletion.create()`. The goal is to provide broad coverage for standard API interactions with minimal setup.

**Important Notes:**

*   **Initialize Once:** Call `wiretapp.monitor()` only one time when your application starts up.
*   **Collector Accessibility:** Your application's environment must be able to send HTTP requests to the `WIRETAPP_ENDPOINT`. Ensure your collector service is running and network configurations (firewalls, Docker networks) allow this communication.
*   **Minimal Impact:** Wiretapp is designed to be lightweight. However, telemetry collection does involve a small amount of processing and a network request for each monitored OpenAI call. If `WIRETAPP_INCLUDE_CONTENT=1` is enabled, the size of the telemetry data sent will increase, which might have a larger performance impact.

## Components

Folder

Description

wiretapp/

Python SDK that monkey‑patches OpenAI calls and emits telemetry

collector/

FastAPI ingestion service that receives telemetry via an HTTP endpoint (e.g. `/events`). It validates events using Pydantic (acting as schema validation) and publishes them to a Kafka topic (e.g., `KAFKA_VALIDATED_EVENTS_TOPIC`). The collector features resilient Kafka integration with automated retries and provides a `/health` endpoint.

spark_jobs/

Components for stream processing of telemetry data (e.g., using Spark), for a more advanced pipeline.

dbt/

Data transformation definitions (e.g., using dbt) for structuring analytics, for a more advanced pipeline.

superset/

Example configurations for visualizing metrics in dashboarding tools like Superset, as part of a full pipeline setup.

docker-compose.yml

One‑command dev and demo stack for core services (Collector, Kafka).

## Default metrics

The Wiretapp SDK collects data points that, with a full pipeline setup (including components like Spark, dbt, and a dashboarding tool), can be used to build metrics such as:

- Daily and weekly active users
- Sessions per user
- Token spend (avg, p95, total)
- Completion latency (p50, p95)
- Error and fallback rates
- Model version usage share

All metrics refresh in under five minutes on commodity hardware.

## Architecture (bird's‑eye)

`Your App  →  Wiretapp SDK  →  HTTP/Kafka  →  Collector  →  Spark Streaming  →  Iceberg on MinIO  →  dbt  →  Superset`

(The full pipeline shown includes components like Spark, Iceberg, dbt, and Superset which typically require further setup and configuration beyond the initial SDK and Collector deployment.)

## Configuration

Wiretapp uses environment variables for secrets and hostnames. Copy `.env.example` to `.env` and tweak.

**SDK Configuration (.env):**

```
# Tells the Wiretapp SDK where to send telemetry data
WIRETAPP_ENDPOINT=http://localhost:8000/events

# Optional: Set to 1 to include raw prompt/completion content (consider privacy implications)
# WIRETAPP_INCLUDE_CONTENT=0
```

**Collector Service Configuration (.env):**

These variables configure the Collector service itself. Defaults are often suitable for local Docker-based setups.

```
# Kafka settings for the Collector
KAFKA_BOOTSTRAP_SERVERS=kafka:9092
KAFKA_VALIDATED_EVENTS_TOPIC=validated-telemetry-events
# KAFKA_RAW_EVENTS_TOPIC=raw-telemetry-events # If using an alternative raw ingestion path

# Collector application host and port (typically managed by Docker/Uvicorn)
# APP_HOST=0.0.0.0
# APP_PORT=8000
```

**Data Pipeline Configuration (.env) (for downstream components):**
```
MINIO_ACCESS_KEY=minio
MINIO_SECRET_KEY=minio123
```

## Privacy and security

- User and session IDs are SHA‑256 hashed client‑side before transport.
- Raw prompts and completions are redacted unless `WIRETAPP_INCLUDE_CONTENT=1` is set.
- Collector enforces Pydantic-based schema validation on incoming data and is designed to allow for request size limit enforcement.

## Roadmap
- TBD

## Contributing
- Pull requests are welcome. Open an issue first to discuss major changes.
- Fork the repo and clone it.
- Create a feature branch.
- Run make test and make lint.
- Submit a PR.