import datetime
import hashlib
import json
import logging
from enum import Enum
from typing import Any, Dict

from pydantic import AnyHttpUrl, BaseModel, Extra, root_validator, validator

logger = logging.getLogger(__name__)


class AlertSeverity(Enum):
    CRITICAL = ("critical", 5)
    HIGH = ("high", 4)
    WARNING = ("warning", 3)
    INFO = ("info", 2)
    LOW = ("low", 1)

    def __new__(cls, severity_name, severity_order):
        obj = object.__new__(cls)
        obj._value_ = severity_name
        obj.severity_order = severity_order
        return obj

    @property
    def order(self):
        return self.severity_order

    def __str__(self):
        return self._value_

    @classmethod
    def from_number(cls, n):
        for severity in cls:
            if severity.order == n:
                return severity
        raise ValueError(f"No AlertSeverity with order {n}")

    def __lt__(self, other):
        if isinstance(other, AlertSeverity):
            return self.order < other.order
        return NotImplemented

    def __le__(self, other):
        if isinstance(other, AlertSeverity):
            return self.order <= other.order
        return NotImplemented

    def __gt__(self, other):
        if isinstance(other, AlertSeverity):
            return self.order > other.order
        return NotImplemented

    def __ge__(self, other):
        if isinstance(other, AlertSeverity):
            return self.order >= other.order
        return NotImplemented


class AlertStatus(Enum):
    # Active alert
    FIRING = "firing"
    # Alert has been resolved
    RESOLVED = "resolved"
    # Alert has been acknowledged but not resolved
    ACKNOWLEDGED = "acknowledged"
    # Alert is suppressed due to various reasons
    SUPPRESSED = "suppressed"
    # No Data
    PENDING = "pending"


class AlertDto(BaseModel):
    id: str
    name: str
    status: AlertStatus
    severity: AlertSeverity
    lastReceived: str
    environment: str = "undefined"
    isDuplicate: bool | None = None
    duplicateReason: str | None = None
    service: str | None = None
    source: list[str] | None = []
    apiKeyRef: str | None = None
    message: str | None = None
    description: str | None = None
    pushed: bool = False  # Whether the alert was pushed or pulled from the provider
    event_id: str | None = None  # Database alert id
    url: AnyHttpUrl | None = None
    labels: dict | None = {}
    fingerprint: str | None = (
        None  # The fingerprint of the alert (used for alert de-duplication)
    )
    deleted: bool = (
        False  # @tal: Obselete field since we have dismissed, but kept for backwards compatibility
    )
    dismissUntil: str | None = None  # The time until the alert is dismissed
    # DO NOT MOVE DISMISSED ABOVE dismissedUntil since it is used in root_validator
    dismissed: bool = False  # Whether the alert has been dismissed
    assignee: str | None = None  # The assignee of the alert
    providerId: str | None = None  # The provider id
    providerType: str | None = None  # The provider type
    group: bool = False  # Whether the alert is a group alert
    note: str | None = None  # The note of the alert
    startedAt: str | None = (
        None  # The time the alert started - e.g. if alert triggered multiple times, it will be the time of the first trigger (calculated on querying)
    )
    isNoisy: bool = False  # Whether the alert is noisy

    def __str__(self) -> str:
        # Convert the model instance to a dictionary
        model_dict = self.dict()
        return json.dumps(model_dict, indent=4, default=str)

    def __eq__(self, other):
        if isinstance(other, AlertDto):
            # Convert both instances to dictionaries
            dict_self = self.dict()
            dict_other = other.dict()

            # Fields to exclude from comparison since they are bit different in different db's
            # todo: solve it in a better way
            exclude_fields = {"lastReceived", "startedAt", "event_id"}

            # Remove excluded fields from both dictionaries
            for field in exclude_fields:
                dict_self.pop(field, None)
                dict_other.pop(field, None)

            # Compare the dictionaries
            return dict_self == dict_other
        return False

    def __ne__(self, other):
        return not self.__eq__(other)

    @validator("fingerprint", pre=True, always=True)
    def assign_fingerprint_if_none(cls, fingerprint, values):
        # if its none, use the name
        if fingerprint is None:
            fingerprint_payload = values.get("name")
            # if the alert name is None, than use the entire payload
            if not fingerprint_payload:
                logger.warning("No name to alert, using the entire payload")
                fingerprint_payload = json.dumps(values)
            fingerprint = hashlib.sha256(fingerprint_payload.encode()).hexdigest()
        # take only the first 255 characters
        else:
            fingerprint = fingerprint[:255]
        return fingerprint

    @validator("deleted", pre=True, always=True)
    def validate_deleted(cls, deleted, values):
        if isinstance(deleted, bool):
            return deleted
        if isinstance(deleted, list):
            return values.get("lastReceived") in deleted

    @validator("lastReceived", pre=True, always=True)
    def validate_last_received(cls, last_received, values):
        if not last_received:
            last_received = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return last_received

    @validator("dismissed", pre=True, always=True)
    def validate_dismissed(cls, dismissed, values):
        # normzlize dismissed value
        if isinstance(dismissed, str):
            dismissed = dismissed.lower() == "true"

        # if dismissed is False, return False
        if not dismissed:
            return dismissed

        # else, validate dismissedUntil
        dismiss_until = values.get("dismissUntil")
        # if there's no dismissUntil, return just return dismissed
        if not dismiss_until or dismiss_until == "forever":
            return dismissed

        # if there's dismissUntil, validate it
        dismiss_until_datetime = datetime.datetime.strptime(
            dismiss_until, "%Y-%m-%dT%H:%M:%S.%fZ"
        ).replace(tzinfo=datetime.timezone.utc)
        dismissed = (
            datetime.datetime.now(datetime.timezone.utc) < dismiss_until_datetime
        )
        return dismissed

    @root_validator(pre=True)
    def set_default_values(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        # Check and set default severity
        severity = values.get("severity")
        try:
            # if severity is int, convert it to AlertSeverity
            if isinstance(severity, int):
                values["severity"] = AlertSeverity.from_number(severity)
            else:
                values["severity"] = AlertSeverity(severity)
        except ValueError:
            logging.warning(
                f"Invalid severity value: {severity}, setting default.",
                extra={"event": values},
            )
            values["severity"] = AlertSeverity.INFO

        # Check and set default status
        status = values.get("status")
        try:
            values["status"] = AlertStatus(status)
        except ValueError:
            logging.warning(
                f"Invalid status value: {status}, setting default.",
                extra={"event": values},
            )
            values["status"] = AlertStatus.FIRING

        # this is code duplication of enrichment_helpers.py and should be refactored
        lastReceived = values["lastReceived"]
        assignees = values.pop("assignees", None)
        if assignees:
            dt = datetime.datetime.fromisoformat(lastReceived)
            dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            assignee = assignees.get(lastReceived) or assignees.get(dt)
            values["assignee"] = assignee
        values.pop("deletedAt", None)
        return values

    # after root_validator to ensure that the values are set
    @root_validator(pre=False)
    def validate_status(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        # if dismissed, change status to SUPPRESSED
        # note this is happen AFTER validate_dismissed which already consider
        #   dismissed + dismissUntil
        if values.get("dismissed"):
            values["status"] = AlertStatus.SUPPRESSED
        return values

    class Config:
        extra = Extra.allow
        schema_extra = {
            "examples": [
                {
                    "id": "1234",
                    "name": "Alert name",
                    "status": "firing",
                    "lastReceived": "2021-01-01T00:00:00.000Z",
                    "environment": "production",
                    "isDuplicate": False,
                    "duplicateReason": None,
                    "service": "backend",
                    "source": ["keep"],
                    "message": "Keep: Alert message",
                    "description": "Keep: Alert description",
                    "severity": "critical",
                    "pushed": True,
                    "event_id": "1234",
                    "url": "https://www.keephq.dev?alertId=1234",
                    "labels": {"key": "value"},
                    "ticket_url": "https://www.keephq.dev?enrichedTicketId=456",
                    "fingerprint": "1234",
                }
            ]
        }
        use_enum_values = True
        json_encoders = {
            # Converts enums to their values for JSON serialization
            Enum: lambda v: v.value,
        }


class DeleteRequestBody(BaseModel):
    fingerprint: str
    lastReceived: str
    restore: bool = False


class DismissRequestBody(BaseModel):
    fingerprint: str
    dismissUntil: str
    dismissComment: str
    restore: bool = False


class EnrichAlertRequestBody(BaseModel):
    enrichments: dict[str, str]
    fingerprint: str
