import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from config import settings
from fastapi import BackgroundTasks, FastAPI, HTTPException
from kafka.admin import KafkaAdminClient, NewTopic
from kafka.consumer import KafkaConsumer
from kafka.errors import NoBrokersAvailable, TopicAlreadyExistsError
from kafka.producer import KafkaProducer
from pydantic import ValidationError
from schemas import TelemetryEvent

# Setup basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Kafka connection retry settings
MAX_KAFKA_RETRIES = 10
KAFKA_RETRY_DELAY_SECONDS = 5

@dataclass
class CollectorState:
    """Global state for the collector service."""
    kafka_producer: KafkaProducer | None = None
    kafka_consumer_task: asyncio.Task | None = None
    kafka_init_task: asyncio.Task | None = None
    kafka_topic_creation_task: asyncio.Task | None = None

_state = CollectorState()

def _blocking_create_kafka_topics():
    """Synchronous part of creating Kafka topics with retries."""
    admin_client = None
    for attempt in range(MAX_KAFKA_RETRIES):
        try:
            logger.info(
                f"Attempting to connect to Kafka admin client (attempt {attempt + 1}/{MAX_KAFKA_RETRIES}) at {settings.KAFKA_BOOTSTRAP_SERVERS}"
            )
            admin_client = KafkaAdminClient(
                bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS.split(","),
                client_id="wiretapp-collector-admin-retry",  # Unique client ID for retrying admin
                request_timeout_ms=15000,  # Timeout for individual admin client operations
                retry_backoff_ms=1000,  # Backoff for admin client's internal retries
            )
            topics_to_create = [
                NewTopic(
                    name=settings.KAFKA_RAW_EVENTS_TOPIC,
                    num_partitions=1,
                    replication_factor=1,
                ),
                NewTopic(
                    name=settings.KAFKA_VALIDATED_EVENTS_TOPIC,
                    num_partitions=1,
                    replication_factor=1,
                ),
            ]
            logger.info(f"Creating topics: {[t.name for t in topics_to_create]}")
            admin_client.create_topics(new_topics=topics_to_create, validate_only=False)
            logger.info("Kafka topics ensured.")
            return  # Success, exit loop
        except TopicAlreadyExistsError:
            logger.info("Kafka topics already exist.")
            return  # Success (topics are there), exit loop
        except NoBrokersAvailable:
            logger.warning(
                f"Attempt {attempt + 1}/{MAX_KAFKA_RETRIES} failed: Could not connect to Kafka admin client at {settings.KAFKA_BOOTSTRAP_SERVERS}."
            )
            if attempt < MAX_KAFKA_RETRIES - 1:
                logger.info(f"Retrying in {KAFKA_RETRY_DELAY_SECONDS} seconds...")
                time.sleep(KAFKA_RETRY_DELAY_SECONDS)
            else:
                logger.error(
                    f"All {MAX_KAFKA_RETRIES} attempts to connect to Kafka for topic creation failed."
                )
                break  # All retries failed, exit loop
        except Exception as e:
            logger.error(
                f"Failed to create Kafka topics on attempt {attempt + 1}/{MAX_KAFKA_RETRIES}: {e}"
            )
            break  # Other critical error, exit loop
        finally:
            if admin_client:
                admin_client.close()
                admin_client = None  # Ensure it's recreated on retry


async def _create_kafka_topics_if_not_exist():
    """Creates the necessary Kafka topics if they don't already exist, using a thread for blocking calls."""
    await asyncio.to_thread(_blocking_create_kafka_topics)


def _blocking_init_kafka_producer():
    """Synchronous part of initializing the Kafka producer with retries."""
    for attempt in range(MAX_KAFKA_RETRIES):
        try:
            logger.info(
                f"Attempting to initialize Kafka producer (attempt {attempt + 1}/{MAX_KAFKA_RETRIES}) for brokers: {settings.KAFKA_BOOTSTRAP_SERVERS}"
            )
            _state.kafka_producer = KafkaProducer(
                bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS.split(","),
                value_serializer=lambda v: json.dumps(v.__dict__, default=str).encode("utf-8"),
                acks="all",
                retries=3,
                request_timeout_ms=15000,
                reconnect_backoff_ms=1000,
                reconnect_backoff_max_ms=10000,
                metadata_max_age_ms=30000,
            )
            logger.info("Kafka producer initialized successfully.")
            return
        except NoBrokersAvailable:
            logger.warning(
                f"Attempt {attempt + 1}/{MAX_KAFKA_RETRIES} failed: Could not connect to Kafka brokers at {settings.KAFKA_BOOTSTRAP_SERVERS} for producer initialization."
            )
            if attempt < MAX_KAFKA_RETRIES - 1:
                logger.info(f"Retrying in {KAFKA_RETRY_DELAY_SECONDS} seconds...")
                time.sleep(KAFKA_RETRY_DELAY_SECONDS)
            else:
                logger.error(
                    f"All {MAX_KAFKA_RETRIES} attempts to initialize Kafka producer failed. Producer not initialized."
                )
                _state.kafka_producer = None
                break
        except Exception as e:
            logger.error(
                f"Failed to initialize Kafka producer on attempt {attempt + 1}/{MAX_KAFKA_RETRIES}: {e}"
            )
            _state.kafka_producer = None
            break


async def _init_kafka_producer() -> None:
    """Initializes the Kafka producer if not already initialized, using a thread for blocking calls."""
    if _state.kafka_producer is None:
        await asyncio.to_thread(_blocking_init_kafka_producer)


async def _get_kafka_producer() -> KafkaProducer | None:
    """Gets a Kafka producer instance, ensuring it's initialized (async)."""
    if _state.kafka_producer is None:
        if _state.kafka_init_task and _state.kafka_init_task.done() and _state.kafka_init_task.exception():
            logger.warning(
                "Kafka producer initialization previously failed. Not re-attempting automatically in _get_kafka_producer."
            )
        elif _state.kafka_init_task and not _state.kafka_init_task.done():
            logger.info(
                "Kafka producer initialization is still in progress. Waiting for it to complete..."
            )
            await _state.kafka_init_task
        else:
            logger.info(
                "Kafka producer is None. Attempting to initialize now via _init_kafka_producer."
            )
            await _init_kafka_producer()

    return _state.kafka_producer


async def _process_kafka_message(message: Any, producer: KafkaProducer | None) -> None:
    """Process a single Kafka message."""
    try:
        logger.debug(f"Received message from Kafka: {message.value}")
        event_data = TelemetryEvent(**message.value)
        logger.info(f"Successfully validated event: {event_data.event_id} from Kafka topic {settings.KAFKA_RAW_EVENTS_TOPIC}")

        if producer:
            producer.send(settings.KAFKA_VALIDATED_EVENTS_TOPIC, value=event_data)
            producer.flush()
            logger.info(f"Event {event_data.event_id} sent to Kafka topic {settings.KAFKA_VALIDATED_EVENTS_TOPIC}")
        else:
            logger.warning("Kafka producer not available. Cannot send validated event to Kafka.")

    except ValidationError as ve:
        logger.error(f"Validation error for event from Kafka: {ve}. Event: {message.value}")
    except json.JSONDecodeError as je:
        logger.error(f"JSON decode error for message from Kafka: {je}. Raw message: {message.value}")
    except Exception as e:
        logger.error(f"Error processing message from Kafka: {e}. Message: {message.value}")


async def _kafka_consumer_job():
    """Consumes messages from the raw events topic, validates, and produces to validated topic (with retries)."""
    consumer = None
    for attempt in range(MAX_KAFKA_RETRIES):
        try:
            logger.info(f"Attempting to initialize Kafka consumer (attempt {attempt + 1}/{MAX_KAFKA_RETRIES}) for topic '{settings.KAFKA_RAW_EVENTS_TOPIC}' at brokers {settings.KAFKA_BOOTSTRAP_SERVERS}")
            consumer = KafkaConsumer(
                settings.KAFKA_RAW_EVENTS_TOPIC,
                bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS.split(","),
                auto_offset_reset="earliest",
                group_id="wiretapp-collector-group",
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                request_timeout_ms=15000,
                reconnect_backoff_ms=1000,
                reconnect_backoff_max_ms=10000,
                consumer_timeout_ms=5000,
            )
            logger.info("Kafka consumer initialized. Starting to listen for messages...")

            producer = await _get_kafka_producer()

            for message in consumer:
                await _process_kafka_message(message, producer)

            logger.info("Kafka consumer message loop finished.")
            return

        except NoBrokersAvailable:
            logger.warning(f"Attempt {attempt + 1}/{MAX_KAFKA_RETRIES} failed: Kafka consumer could not connect to brokers at {settings.KAFKA_BOOTSTRAP_SERVERS}.")
            if consumer:
                consumer.close()
                consumer = None
            if attempt < MAX_KAFKA_RETRIES - 1:
                logger.info(f"Retrying in {KAFKA_RETRY_DELAY_SECONDS} seconds...")
                await asyncio.sleep(KAFKA_RETRY_DELAY_SECONDS)
            else:
                logger.error(f"All {MAX_KAFKA_RETRIES} attempts to initialize Kafka consumer failed. Consumer job exiting.")
                break
        except Exception as e:
            logger.error(f"Kafka consumer job encountered an unhandled exception during initialization or processing (attempt {attempt + 1}/{MAX_KAFKA_RETRIES}): {e}")
            if consumer:
                consumer.close()
                consumer = None
            break

    if consumer:
        consumer.close()
    logger.info("Kafka consumer job has shut down.")


async def _shutdown_tasks() -> None:
    """Handle graceful shutdown of all tasks and connections."""
    if _state.kafka_topic_creation_task and not _state.kafka_topic_creation_task.done():
        try:
            logger.info("Waiting for Kafka topic creation task to complete...")
            await asyncio.wait_for(_state.kafka_topic_creation_task, timeout=10)
            logger.info("Kafka topic creation task completed.")
        except (TimeoutError, Exception) as e:
            logger.error(f"Error during Kafka topic creation task shutdown: {e}")
            raise RuntimeError("Failed to shutdown Kafka topic creation task") from e

    if _state.kafka_init_task and not _state.kafka_init_task.done():
        try:
            logger.info("Waiting for Kafka init task to complete...")
            await asyncio.wait_for(_state.kafka_init_task, timeout=10)
            logger.info("Kafka init task completed.")
        except (TimeoutError, Exception) as e:
            logger.error(f"Error during Kafka init task shutdown: {e}")
            raise RuntimeError("Failed to shutdown Kafka init task") from e

    if _state.kafka_producer:
        logger.info("Closing Kafka producer...")
        await asyncio.to_thread(_state.kafka_producer.close)
        logger.info("Kafka producer closed.")
    else:
        logger.info("Kafka producer was not initialized, skipping close.")

    if _state.kafka_consumer_task and not _state.kafka_consumer_task.done():
        logger.info("Cancelling Kafka consumer task...")
        _state.kafka_consumer_task.cancel()
        try:
            await _state.kafka_consumer_task
        except asyncio.CancelledError:
            logger.info("Kafka consumer task successfully cancelled.")
        except Exception as e:
            logger.error(f"Error during Kafka consumer task shutdown: {e}")
            raise RuntimeError("Failed to shutdown Kafka consumer task") from e

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handles startup and shutdown events for the FastAPI application."""
    logger.info("Collector service starting up...")

    logger.info("Scheduling Kafka topic creation as a background task...")
    _state.kafka_topic_creation_task = asyncio.create_task(
        _create_kafka_topics_if_not_exist(), name="kafka_topic_creation"
    )
    _state.kafka_topic_creation_task.add_done_callback(_handle_task_result)

    logger.info("Scheduling Kafka producer initialization as a background task...")
    _state.kafka_init_task = asyncio.create_task(
        _init_kafka_producer(), name="kafka_producer_init"
    )
    _state.kafka_init_task.add_done_callback(_handle_task_result)

    if settings.KAFKA_BOOTSTRAP_SERVERS:
        logger.info("Scheduling Kafka consumer job as a background task...")
        _state.kafka_consumer_task = asyncio.create_task(
            _kafka_consumer_job(), name="kafka_consumer_job"
        )
        _state.kafka_consumer_task.add_done_callback(_handle_task_result)
    else:
        logger.info("KAFKA_BOOTSTRAP_SERVERS not configured. Kafka consumer will not start.")

    yield

    logger.info("Collector service shutting down...")
    await _shutdown_tasks()
    logger.info("Collector service shutdown complete.")


# Callback to handle results of background tasks
def _handle_task_result(task: asyncio.Task):
    try:
        task.result()  # Raise exception if task failed
        logger.info(f"Background task {task.get_name()} completed successfully.")
    except asyncio.CancelledError:
        logger.info(f"Background task {task.get_name()} was cancelled.")
    except Exception as e:
        logger.error(f"Background task {task.get_name()} failed: {e}", exc_info=True)


app = FastAPI(lifespan=lifespan, title="Wiretapp Collector Service")


@app.get("/health", summary="Health check endpoint")
async def health_check():
    """Provides a simple health check endpoint."""
    # Could be expanded to check Kafka connection status if critical
    return {"status": "ok"}


@app.post("/events", summary="Ingest a single telemetry event via HTTP")
async def http_ingest_event(event: TelemetryEvent, background_tasks: BackgroundTasks):
    """
    Receives a single telemetry event via HTTP POST.
    Validates the event and, if valid, forwards it to the validated Kafka topic.
    """
    logger.info(f"Received event via HTTP: {event.event_id}")

    producer = await _get_kafka_producer()  # Get producer (now async)
    if producer:
        try:
            # Run the blocking send and flush in a separate thread
            await asyncio.to_thread(
                producer.send, settings.KAFKA_VALIDATED_EVENTS_TOPIC, value=event
            )
            await asyncio.to_thread(producer.flush)
            logger.info(
                f"Event {event.event_id} sent to Kafka topic {settings.KAFKA_VALIDATED_EVENTS_TOPIC} from HTTP endpoint."
            )
            return {
                "status": "event received and sent to processing",
                "event_id": event.event_id,
            }
        except Exception as e:
            logger.error(
                f"Failed to send event {event.event_id} to Kafka from HTTP endpoint: {e}"
            )
            raise HTTPException(
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR, detail=f"Error sending event to Kafka: {e!s}"
            ) from e
    else:
        logger.warning(
            f"Kafka producer not available. Event {event.event_id} received via HTTP but not sent to Kafka."
        )
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="Service temporarily unable to process events due to Kafka unavailability.",
        ) from None


if __name__ == "__main__":
    # This is for local development running directly with `python main.py`
    # For production, use `uvicorn main:app --host 0.0.0.0 --port 8000` as in Dockerfile
    import uvicorn

    logger.info("Starting Uvicorn server for local development...")
    uvicorn.run(app, host=settings.APP_HOST, port=settings.APP_PORT)
