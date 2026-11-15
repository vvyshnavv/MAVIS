import os
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

TRANSCRIPTS_DIR = Path(__file__).resolve().parents[2] / "transcripts"
TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

class CallTranscriptRecorder:
    """
    Records full timestamped conversation turns for MAVIS call sessions.
    Saves both structured JSON and human-readable TXT files upon call updates.
    """
    _active_recorders: Dict[str, "CallTranscriptRecorder"] = {}

    def __init__(self, call_id: str, caller_phone: str, hospital_name: str = "MAVIS Hospital"):
        self.call_id = call_id
        self.caller_phone = caller_phone
        self.hospital_name = hospital_name
        self.call_started_at = datetime.now(timezone.utc)
        self.entries: List[Dict[str, str]] = []
        self.booking_data: Optional[Dict[str, Any]] = None
        self.emergency_data: Optional[Dict[str, Any]] = None

    @classmethod
    def get_or_create(cls, call_id: str, caller_phone: str) -> "CallTranscriptRecorder":
        if call_id not in cls._active_recorders:
            cls._active_recorders[call_id] = CallTranscriptRecorder(call_id, caller_phone)
        return cls._active_recorders[call_id]

    def add_turn(self, speaker: str, text: str):
        cleaned = text.strip()
        if not cleaned:
            return
        self.entries.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "speaker": speaker, # 'caller' or 'mavis'
            "text": cleaned
        })
        self.save_to_disk()

    def record_booking(self, booking: Dict[str, Any]):
        self.booking_data = booking
        self.save_to_disk()

    def record_emergency(self, emergency: Dict[str, Any]):
        self.emergency_data = emergency
        self.save_to_disk()

    def save_to_disk(self):
        if not self.entries:
            return
        
        safe_caller = re.sub(r"[^A-Za-z0-9._-]+", "-", self.caller_phone).strip("-") or "caller"
        safe_call_id = re.sub(r"[^A-Za-z0-9._-]+", "-", self.call_id).strip("-")
        base_filename = f"{self.call_started_at.strftime('%Y%m%d_%H%M%S')}_{safe_call_id}_{safe_caller}"
        
        json_path = TRANSCRIPTS_DIR / f"{base_filename}.json"
        txt_path = TRANSCRIPTS_DIR / f"{base_filename}.txt"

        payload = {
            "header": {
                "call_id": self.call_id,
                "caller_phone": self.caller_phone,
                "hospital": self.hospital_name,
                "call_started_at": self.call_started_at.isoformat()
            },
            "entries": self.entries,
            "booking": self.booking_data,
            "emergency": self.emergency_data
        }

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

        lines = [
            "============================================================",
            f" MAVIS Hospital Voice Intake Call Transcript",
            f" Call ID: {self.call_id} | Caller: {self.caller_phone}",
            f" Started: {self.call_started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "============================================================",
            ""
        ]
        for e in self.entries:
            time_str = datetime.fromisoformat(e["timestamp"]).strftime("%H:%M:%S")
            speaker_tag = "CALLER" if e["speaker"] == "caller" else "MAVIS"
            lines.append(f"[{time_str}] {speaker_tag}: {e['text']}")

        if self.booking_data:
            lines.extend([
                "",
                "--- BOOKING CONFIRMATION ---",
                f"Token ID: {self.booking_data.get('token_id')}",
                f"Department: {self.booking_data.get('department')}",
                f"Doctor: {self.booking_data.get('doctor_name')} ({self.booking_data.get('room_number')})",
                f"Queue Position: #{self.booking_data.get('queue_position')}"
            ])

        if self.emergency_data:
            lines.extend([
                "",
                "--- 🚨 EMERGENCY SAFETY GATE TRIGGERED ---",
                f"Trigger: {self.emergency_data.get('trigger_phrase')}",
                f"Rationale: {self.emergency_data.get('clinical_rationale')}",
                f"Audit Hash: {self.emergency_data.get('record_hash')}"
            ])

        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

transcript_service = CallTranscriptRecorder

