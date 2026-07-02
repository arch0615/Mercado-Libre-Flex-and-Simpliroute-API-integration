"""Mercado Libre webhook receiver — the low-latency path.

ML posts a notification the instant an order/shipment changes. We ack with
200 immediately (ML retries on non-2xx and expects a fast reply) and process
the order in a background task, so a visit shows up in SimpliRoute seconds
after the sale instead of waiting for the next cron tick.

Safety: the background task runs the same idempotent `process_order` as the
cron, so a notification racing the cron can never create a duplicate visit.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Request

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import logger
from app.scheduler.ondemand import process_by_order_id, process_by_shipment_id

router = APIRouter(prefix="/ml", tags=["ml"])

# ML topics that map to an order resource (/orders/{id}) ...
_ORDER_TOPICS = {"orders_v2", "orders", "created_orders"}
# ... and to a shipment resource (/shipments/{id}).
_SHIPMENT_TOPICS = {"shipments"}


@router.post("/notifications", summary="Mercado Libre webhook receiver")
async def notifications(request: Request, background: BackgroundTasks) -> dict:
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    topic = str(payload.get("topic") or "")
    resource = str(payload.get("resource") or "")
    user_id = payload.get("user_id")

    settings = get_settings()
    # Only act on our own seller's notifications when a seller id is configured.
    if settings.ml_seller_id and str(user_id) != str(settings.ml_seller_id):
        logger.info("ml_notification_ignored_user", user_id=user_id, topic=topic)
        return {"status": "ignored"}

    resource_id = resource.rstrip("/").rsplit("/", 1)[-1] if resource else ""
    if topic in _ORDER_TOPICS and resource_id:
        background.add_task(_process_order, resource_id)
    elif topic in _SHIPMENT_TOPICS and resource_id:
        background.add_task(_process_shipment, resource_id)
    else:
        logger.info("ml_notification_skipped_topic", topic=topic, resource=resource)
        return {"status": "skipped", "topic": topic}

    return {"status": "accepted", "topic": topic, "resource_id": resource_id}


def _process_order(ml_order_id: str) -> None:
    try:
        with session_scope() as session:
            result = process_by_order_id(session, ml_order_id)
        logger.info(
            "ml_notification_processed",
            ml_order_id=ml_order_id,
            found=result.found,
            outcome=result.outcome,
        )
    except Exception:  # never let a background failure crash the worker
        logger.exception("ml_notification_process_failed", ml_order_id=ml_order_id)


def _process_shipment(shipment_id: str) -> None:
    try:
        with session_scope() as session:
            result = process_by_shipment_id(session, shipment_id)
        logger.info(
            "ml_notification_processed_shipment",
            shipment_id=shipment_id,
            found=result.found,
            outcome=result.outcome,
        )
    except Exception:
        logger.exception(
            "ml_notification_process_failed_shipment", shipment_id=shipment_id
        )
