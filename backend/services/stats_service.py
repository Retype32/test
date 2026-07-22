import uuid
from datetime import date
from decimal import Decimal
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from ..repositories.stats_repository import StatsRepository

TARGET_SLIPS_PER_HOUR = 23


def _hours_span(first_txn_at, last_txn_at) -> float:
    span_hours = (last_txn_at - first_txn_at).total_seconds() / 3600
    return max(span_hours, 1.0)


class StatsService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = StatsRepository(db)

    async def get_processor_stats(
        self, date_from: date, date_to: date, user_id: Optional[uuid.UUID] = None
    ) -> list[dict]:
        daily_rows = await self.repo.daily_activity(date_from, date_to, user_id)
        duplicate_counts = await self.repo.confirmed_duplicate_counts(date_from, date_to, user_id)
        transfer_counts = await self.repo.transfer_counts(date_from, date_to, user_id)

        by_user: dict[uuid.UUID, dict] = {}
        for row in daily_rows:
            uid = row["user_id"]
            entry = by_user.setdefault(uid, {
                "user_id": uid,
                "username": row["username"],
                "slip_count": 0,
                "cash_volume": Decimal("0"),
                "active_hours": 0.0,
            })
            entry["slip_count"] += row["slip_count"]
            entry["cash_volume"] += row["cash_volume"] or Decimal("0")
            entry["active_hours"] += _hours_span(row["first_txn_at"], row["last_txn_at"])

        results = []
        for uid, entry in by_user.items():
            duplicate_error_count = duplicate_counts.get(uid, 0)
            transfer_error_count = transfer_counts.get(uid, 0)
            slips_per_hour = entry["slip_count"] / entry["active_hours"] if entry["active_hours"] else 0.0

            results.append({
                "user_id": uid,
                "username": entry["username"],
                "date_from": date_from,
                "date_to": date_to,
                "slip_count": entry["slip_count"],
                "slips_per_hour": round(slips_per_hour, 2),
                "target_per_hour": TARGET_SLIPS_PER_HOUR,
                "cash_volume": entry["cash_volume"],
                "duplicate_error_count": duplicate_error_count,
                "transfer_error_count": transfer_error_count,
                "error_count": duplicate_error_count + transfer_error_count,
            })

        results.sort(key=lambda r: r["username"])
        return results
