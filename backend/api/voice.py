import base64
import uuid
import time
import re
import io
import logging
import secrets
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.session import get_db, AsyncSessionLocal
from backend.services.audio_pipeline import vad_processor, asr_engine, tts_engine, telephony_gateway
from backend.agents.safety_gate import safety_gate
from backend.agents.receptionist import receptionist_agent
from backend.agents.triage_agent import triage_agent
from backend.agents.handoff_agent import handoff_agent
from backend.services.scheduling import scheduling_service
from backend.services.redis_service import redis_service
from backend.services.audit import audit_service
from backend.services.transcript_service import CallTranscriptRecorder
from backend.config import settings

logger = logging.getLogger("mavis.voice")
router = APIRouter(prefix="/voice", tags=["Voice Telephony Pipeline"])

# Department keyword mapping matching voice model specification
VOICE_DEPARTMENTS = {
    "Cardiology": {
        "keywords": ["chest pain", "heart", "palpitation", "blood pressure", "cardiac"],
    },
    "Orthopedics": {
        "keywords": ["fracture", "joint pain", "back pain", "knee", "bone", "sprain", "shoulder", "arthritis"],
    },
    "Pediatrics": {
        "keywords": ["child", "baby", "infant", "kid", "toddler"],
    },
    "ENT": {
        "keywords": ["ear", "nose", "throat", "sinus", "hearing", "earache", "tonsil"],
    },
    "Dermatology": {
        "keywords": ["skin", "rash", "itching", "acne", "eczema", "hives"],
    },
    "Gynecology": {
        "keywords": ["pregnan", "period", "menstrual", "cramp"],
    },
    "Neurology": {
        "keywords": ["headache", "migraine", "seizure", "numbness", "dizz", "vertigo"],
    },
    "Gastroenterology": {
        "keywords": ["stomach", "abdomen", "vomit", "diarrh", "nausea", "acidity", "indigestion"],
    },
    "Psychiatry": {
        "keywords": ["anxious", "depress", "stress", "sleep problem", "panic", "insomnia"],
    },
    "Ophthalmology": {
        "keywords": ["eye", "vision", "blur", "seeing", "cataract", "red eye"],
    },
    "General Medicine": {
        "keywords": ["fever", "cough", "cold", "flu", "weakness", "body pain", "fatigue"],
    },
}

def route_department(symptom_text: str) -> str:
    """Select department with deterministic regex keyword matching, defaulting to General Medicine."""
    lowered = symptom_text.lower()
    for department, info in VOICE_DEPARTMENTS.items():
        if department == "General Medicine":
            continue
        for keyword in info["keywords"]:
            # Word boundary matching prevents substring false positives (e.g. 'severe' matching 'ear')
            pattern = r"\b" + re.escape(keyword) + (r"\b" if len(keyword) <= 4 else r"")
            if re.search(pattern, lowered):
                return department
    return "General Medicine"

class VoiceSessionStartRequest(BaseModel):
    caller_phone: Optional[str] = None
    client_type: Optional[str] = "webrtc_browser"

class LiveKitStartCallRequest(BaseModel):
    caller_id: str = Field(min_length=3, max_length=32)

class ProcessTurnRequest(BaseModel):
    call_id: str
    caller_phone: Optional[str] = None
    caller_utterance: Optional[str] = ""
    current_stage: Optional[str] = "name"
    patient_name: Optional[str] = "Patient"
    patient_age: Optional[int] = 35
    selected_slot_id: Optional[int] = None
    target_department: Optional[str] = None
    symptom_description: Optional[str] = None
    severity: Optional[int] = 5
    duration: Optional[str] = "Recent"
    audio_base64: Optional[str] = None

class TTSSynthesizeRequest(BaseModel):
    text: str

def parse_booking_choice(text: str) -> Optional[bool]:
    """Parse spoken yes/no variants; returns True, False, or None for ambiguous answers."""
    answer = re.sub(r"[^a-z0-9']+", " ", text.lower().replace("’", "'")).strip()
    answer = re.sub(r"\s+", " ", answer)
    if not answer:
        return None

    negative_phrases = (
        "not now", "not interested", "not today", "rather not", "don't want",
        "do not want", "would not", "wouldn't", "no thanks", "no thank you",
        "cancel", "stop", "never mind", "nevermind", "no need", "decline"
    )
    if any(phrase in answer for phrase in negative_phrases):
        return False
    if answer == "no problem":
        return True
    if re.match(r"^(no|nope|nah|negative)\b", answer):
        return False

    affirmative_starts = (
        "yes", "yeah", "yep", "yup", "sure", "okay", "ok", "absolutely",
        "definitely", "please do", "go ahead", "book it", "book me", "i would",
        "i'd", "i want", "i'd like", "that works", "sounds good", "that's fine",
        "thats fine", "confirm", "proceed", "schedule", "please book", "book appointment"
    )
    if any(answer == phrase or answer.startswith(phrase + " ") for phrase in affirmative_starts):
        return True
    return None

NAME_EXCLUSIONS = {
    "yes", "no", "hello", "hi", "hey", "patient", "sure", "okay", "thanks",
    "thank you", "thank you.", "thank you so much", "thank you very much",
    "bye", "goodbye", "you", "nothing", "none", "fine", "good", "well",
    "uh", "um", "sorry", "repeat", "pardon", "what", "who", "ok", "yep",
    "nope", "yeah", "the", "and", "for", "with"
}

def extract_name_from_utterance(text: str) -> Optional[str]:
    # Strip trailing punctuation (. , ! ?) while preserving internal letters and spaces
    clean = re.sub(r"[^\w\s]", "", text).strip()
    if not clean:
        return None
    patterns = [
        r"(?:my\s+(?:full\s+)?name\s+is|i\s+am|im|this\s+is|call\s+me|it\s+is|its)\s+([A-Za-z]+(?:\s+[A-Za-z]+){0,2})",
        r"^([A-Za-z]{2,}(?:\s+[A-Za-z]{2,}){0,2})$"
    ]
    for p in patterns:
        m = re.search(p, clean, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
            if candidate.lower() not in NAME_EXCLUSIONS:
                if len(candidate) >= 2 and candidate.lower() not in ["the", "and", "for", "with"]:
                    return candidate.title()
    # Fallback: if caller says 1 to 3 alpha words, treat as patient name
    words = clean.split()
    if 1 <= len(words) <= 3:
        if all(w.isalpha() and w.lower() not in NAME_EXCLUSIONS for w in words):
            return " ".join(words).title()
    return None

def extract_age_from_utterance(text: str) -> Optional[int]:
    clean = re.sub(r"[^\w\s]", " ", text)
    m = re.search(r"\b(\d{1,2})\s*(?:years?\s*old|yo|y/o|age|years)?\b", clean.lower())
    if m:
        try:
            val = int(m.group(1))
            if 1 <= val <= 115:
                return val
        except ValueError:
            pass
    word_map = {
        "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
        "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
        "twenty": 20, "twenty five": 25, "thirty": 30, "thirty five": 35,
        "forty": 40, "forty five": 45, "fifty": 50, "fifty five": 55,
        "sixty": 60, "sixty five": 65, "seventy": 70, "eighty": 80
    }
    for word, val in word_map.items():
        if re.search(r"\b" + re.escape(word) + r"\b", clean.lower()):
            return val
    return None

def extract_severity_from_utterance(text: str) -> Optional[int]:
    m = re.search(r"\b([1-9]|10)\b", text)
    if m:
        try:
            val = int(m.group(1))
            if 1 <= val <= 10:
                return val
        except ValueError:
            pass
    return None

async def execute_voice_turn(
    call_id: str,
    caller_phone: str,
    caller_utterance: Optional[str] = None,
    audio_base64: Optional[str] = None,
    current_stage: str = "name",
    patient_name: str = "Patient",
    patient_age: int = 35,
    target_department: Optional[str] = None,
    symptom_description: Optional[str] = None,
    severity: int = 5,
    duration: str = "Recent",
    db: Optional[AsyncSession] = None
) -> Dict[str, Any]:
    """
    Core non-blocking conversational turn processor.
    Handles non-blocking ASR, synchronous emergency gating, clinical routing, atomic booking, and female TTS.
    """
    utterance = (caller_utterance or "").strip()

    # Step A: Non-blocking ASR Transcription if audio payload is provided
    if audio_base64:
        try:
            raw_b64 = audio_base64
            if "," in raw_b64:
                raw_b64 = raw_b64.split(",", 1)[1]
            audio_bytes = base64.b64decode(raw_b64)
            transcription = await asr_engine.transcribe_audio_chunk_async(audio_bytes, filename="caller_utterance.wav")
            if transcription:
                utterance = transcription
                logger.info("Decoded & transcribed caller utterance (%d bytes): '%s'", len(audio_bytes), utterance)
        except Exception as e:
            logger.error("Audio decode/ASR transcription error: %s", e)

    curr_stage = current_stage or "name"

    # Filter out Whisper silence hallucinations (e.g. "Thank you.", "you") on intake stages
    clean_check = utterance.lower().strip().strip(".!,?")
    if clean_check in {"thank you", "thanks", "you", "bye", "goodbye", "subtitles", "watching", "the end", "subscribe"}:
        if curr_stage in ["reception", "name", "age", "symptom", "severity", "history"]:
            logger.warning("Filtered Whisper silence hallucination ('%s') at stage '%s'", utterance, curr_stage)
            utterance = ""

    # If completely empty audio (ambient background noise/silence), prompt gently for current stage
    if not utterance:
        if curr_stage in ["reception", "name"]:
            gentle_prompt = "I'm listening. What is your full name?"
        elif curr_stage == "age":
            gentle_prompt = "Could you please tell me your age in years?"
        elif curr_stage == "symptom":
            gentle_prompt = "What problem or symptom brought you in today? Please describe it in your own words."
        elif curr_stage == "severity":
            gentle_prompt = "On a scale from 1 to 10, how severe does it feel?"
        elif curr_stage == "history":
            gentle_prompt = "How long has this been happening?"
        elif curr_stage == "booking_confirm":
            gentle_prompt = "Please answer yes or no. Would you like me to book an appointment for you?"
        else:
            gentle_prompt = "I'm listening. Please go ahead and speak into your microphone."

        audio_bytes, audio_mime = await tts_engine.synthesize_async(gentle_prompt)
        return {
            "call_id": call_id,
            "caller_utterance": "",
            "stage": curr_stage,
            "patient_name": patient_name,
            "patient_age": patient_age,
            "symptom_description": symptom_description,
            "severity": severity,
            "duration": duration,
            "target_department": target_department,
            "red_flag_triggered": False,
            "system_speech": gentle_prompt,
            "audio_base64": base64.b64encode(audio_bytes).decode("ascii") if audio_bytes else "",
            "audio_mime": audio_mime,
            "triage_info": None,
            "booking_result": None
        }

    phone = caller_phone or "Not provided"
    recorder = CallTranscriptRecorder.get_or_create(call_id, phone)
    recorder.add_turn("caller", utterance)

    # Step B: Emergency Red-Flag Safety Gate (F4)
    safety_result = await safety_gate.process_safety_check(
        text=utterance,
        call_id=call_id,
        db=db,
        caller_phone=phone
    )

    if safety_result["triggered"]:
        emergency_speech = (
            "Your symptoms may need urgent medical attention. This browser cannot connect "
            "emergency services. Call 112 or 108, or go to the nearest emergency department now."
        )
        recorder.add_turn("mavis", emergency_speech)
        recorder.record_emergency(safety_result)

        audio_bytes, audio_mime = await tts_engine.synthesize_async(emergency_speech)
        return {
            "call_id": call_id,
            "caller_utterance": utterance,
            "stage": "emergency",
            "red_flag_triggered": True,
            "trigger_phrase": safety_result.get("trigger_phrase"),
            "clinical_rationale": safety_result.get("clinical_rationale"),
            "action": "EMERGENCY_ESCALATION",
            "system_speech": emergency_speech,
            "call_bridge_info": safety_result.get("call_bridge_info"),
            "audit_entry_id": safety_result.get("audit_entry_id"),
            "record_hash": safety_result.get("record_hash"),
            "audio_base64": base64.b64encode(audio_bytes).decode("ascii") if audio_bytes else "",
            "audio_mime": audio_mime
        }

    # Step C: State Machine Transitions
    curr_stage = current_stage or "name"
    p_name = patient_name or "Patient"
    p_age = patient_age or 35
    s_desc = symptom_description or ""
    sev = severity or 5
    dur = duration or "Recent"
    dept = target_department
    system_speech = ""
    next_stage = curr_stage
    triage_info = None
    booking_result = None

    # Check for direct multi-field clinical statement (e.g. caller describes symptoms + name/age in one breath)
    extracted = triage_agent._deterministic_extract(utterance)
    dept_match = route_department(utterance)
    has_clinical_keywords = any(w in utterance.lower() for w in [
        "pain", "hurt", "ache", "rash", "fever", "cough", "eye", "vision",
        "chest", "knee", "joint", "swelling", "vomit", "dizzy", "headache",
        "throat", "ear", "blood", "stomach", "cramp", "breath", "sick", "symptom"
    ])
    is_direct_clinical_statement = (
        len(utterance.split()) >= 4 and has_clinical_keywords and (
            (extracted.get("chief_complaint") and extracted.get("chief_complaint") != "General Discomfort") or
            dept_match != "General Medicine"
        )
    )

    if is_direct_clinical_statement and curr_stage in ["reception", "name", "age", "symptom"]:
        if extracted.get("patient_name") and extracted["patient_name"] != "Patient":
            p_name = extracted["patient_name"]
        else:
            found_n = extract_name_from_utterance(utterance)
            if found_n:
                p_name = found_n

        found_a = extracted.get("age") or extract_age_from_utterance(utterance)
        if found_a:
            p_age = found_a

        s_desc = utterance
        sev = extracted.get("severity_scale_1_to_10", 5)
        dur = extracted.get("duration", "Recent")
        dept = dept_match
        next_stage = "booking_confirm"
        system_speech = (
            f"Based on what you shared, the {dept} department may be a fit. "
            f"Would you like me to book an appointment for you?"
        )

    elif curr_stage in ["reception", "name"]:
        found_name = extract_name_from_utterance(utterance)
        if found_name:
            p_name = found_name
            found_age = extract_age_from_utterance(utterance)
            if found_age:
                p_age = found_age
                next_stage = "symptom"
                system_speech = f"Thank you, {p_name}. What problem or symptom brought you in today? Please describe it in your own words."
            else:
                next_stage = "age"
                system_speech = f"Thank you, {p_name}. How old are you?"
        else:
            if len(utterance.split()) >= 4:
                p_name = "Patient"
                s_desc = utterance.strip()
                dept = route_department(s_desc)
                next_stage = "age"
                system_speech = "Thank you. Could you please share your age?"
            else:
                next_stage = "name"
                system_speech = "I didn't quite catch your name. What is your full name?"

    elif curr_stage == "age":
        found_age = extract_age_from_utterance(utterance)
        if found_age:
            p_age = found_age
            next_stage = "symptom"
            system_speech = "What problem or symptom brought you in today? Please describe it in your own words."
        else:
            next_stage = "age"
            system_speech = "Could you please tell me your age in years?"

    elif curr_stage == "symptom":
        clean_s = utterance.strip()
        if len(clean_s.split()) >= 1 and clean_s.lower() not in {"fine", "good", "ok", "okay", "yes", "no"}:
            s_desc = clean_s
            dept = route_department(s_desc)
            next_stage = "severity"
            system_speech = "On a scale from 1 to 10, how severe does it feel?"
        else:
            next_stage = "symptom"
            system_speech = "Could you please describe what symptom or health issue you are experiencing?"

    elif curr_stage == "severity":
        found_sev = extract_severity_from_utterance(utterance)
        if found_sev:
            sev = found_sev
            next_stage = "history"
            system_speech = "How long has this been happening?"
        else:
            next_stage = "severity"
            system_speech = "On a scale from 1 to 10, how severe does it feel? Please state a number between 1 and 10."

    elif curr_stage == "history":
        dur = utterance.strip()
        if not dept:
            dept = route_department(s_desc or dur)
        next_stage = "booking_confirm"
        system_speech = (
            f"Based on what you shared, the {dept} department may be a fit. "
            f"Would you like me to book an appointment for you?"
        )

    elif curr_stage == "clarification":
        combined_utterance = f"{s_desc} - {utterance}"
        dept = route_department(combined_utterance)
        next_stage = "booking_confirm"
        system_speech = (
            f"Based on what you shared, the {dept} department may be a fit. "
            f"Would you like me to book an appointment for you?"
        )

    elif curr_stage == "booking_confirm":
        choice = parse_booking_choice(utterance)
        if choice is True:
            if not dept:
                dept = "General Medicine"

            # Deterministic atomic booking in MAVIS DB
            held_slot = None
            if db:
                held_slot = await scheduling_service.find_and_hold_next_slot(db, department=dept, call_id=call_id)
                if not held_slot and dept != "General Medicine":
                    held_slot = await scheduling_service.find_and_hold_next_slot(db, department="General Medicine", call_id=call_id)
                if not held_slot:
                    held_slot = await scheduling_service.find_and_hold_next_slot(db, department=None, call_id=call_id)

            if held_slot and db:
                booking = await scheduling_service.confirm_booking(
                    db=db,
                    call_id=call_id,
                    slot_id=held_slot["slot_id"],
                    patient_name=p_name,
                    patient_age=p_age,
                    caller_phone=phone,
                    department=dept,
                    triage_data={
                        "chief_complaint": s_desc or "Clinical Consultation",
                        "duration": dur,
                        "severity_scale_1_to_10": sev,
                        "associated_symptoms": [],
                        "triage_confidence": 0.95,
                        "department_rationale": f"Routed based on caller intake: {s_desc}",
                        "urgency_level": "PRIORITY" if sev >= 7 else "ROUTINE",
                        "handoff_brief": handoff_agent.generate_brief(
                            token_id="PENDING",
                            patient_name=p_name,
                            patient_age=p_age,
                            department=dept,
                            doctor_name=held_slot["doctor_name"],
                            slot_timestamp=held_slot["slot_timestamp"],
                            chief_complaint=s_desc or "Clinical Consultation",
                            duration=dur,
                            severity=sev,
                            associated_symptoms=[],
                            triage_confidence=0.95,
                            department_rationale=f"Patient intake: {s_desc}",
                            urgency_level="PRIORITY" if sev >= 7 else "ROUTINE"
                        )
                    }
                )
                booking_result = booking
                recorder.record_booking(booking)
                next_stage = "completed"

                slot_time_str = "today"
                try:
                    slot_dt = held_slot["slot_timestamp"]
                    if hasattr(slot_dt, "strftime"):
                        slot_time_str = slot_dt.strftime("%I:%M %p")
                    elif isinstance(slot_dt, str):
                        slot_time_str = slot_dt
                except Exception:
                    pass

                system_speech = (
                    f"Your appointment is booked. You can check your appointment details in the patient portal. "
                    f"Your doctor is {held_slot['doctor_name']} in {dept} at {slot_time_str}. "
                    f"Your reference token is {booking['token_id']}. "
                    f"Is there anything else you'd like to ask about the details you shared?"
                )
            else:
                next_stage = "completed"
                system_speech = (
                    "I couldn't find an available appointment slot, so no appointment was booked. "
                    "Please contact the hospital to arrange an appointment."
                )

        elif choice is False:
            next_stage = "completed"
            system_speech = "All right. No appointment was booked. Thank you for contacting MAVIS."
        else:
            next_stage = "booking_confirm"
            system_speech = "Please answer yes or no. Would you like me to book an appointment for you?"

    elif curr_stage == "completed":
        q = utterance.lower()
        detail = None
        if any(w in q for w in ("my name", "what name", "name did", "who am i")):
            detail = f"You told me your name is {p_name}."
        elif any(w in q for w in ("my age", "how old", "age did")):
            detail = f"You told me you are {p_age} years old."
        elif any(w in q for w in ("symptom", "problem", "what did i say", "what brought")):
            detail = f"You described: {s_desc}."
        elif any(w in q for w in ("severity", "scale", "rating")):
            detail = f"You rated the severity as {sev} out of 10."
        elif any(w in q for w in ("how long", "history", "duration")):
            detail = f"You said this has been happening for: {dur}."

        if detail is not None:
            system_speech = f"{detail} Thank you. I hope you have a good day."
        else:
            system_speech = "I can help with the information you shared during this call. Thank you. I hope you have a good day."
        next_stage = "completed"

    recorder.add_turn("mavis", system_speech)

    # Synthesize neural female speech audio
    audio_bytes, audio_mime = await tts_engine.synthesize_async(system_speech)
    audio_b64_out = base64.b64encode(audio_bytes).decode("ascii") if audio_bytes else ""

    return {
        "call_id": call_id,
        "caller_utterance": utterance,
        "stage": next_stage,
        "patient_name": p_name,
        "patient_age": p_age,
        "symptom_description": s_desc,
        "severity": sev,
        "duration": dur,
        "target_department": dept,
        "red_flag_triggered": False,
        "system_speech": system_speech,
        "audio_base64": audio_b64_out,
        "audio_mime": audio_mime,
        "triage_info": triage_info,
        "booking_result": booking_result
    }

@router.post("/session/start")
async def start_voice_session(req: VoiceSessionStartRequest, db: AsyncSession = Depends(get_db)):
    """
    Initializes a new voice intake session with the voice model's original greeting and female neural audio.
    """
    call_id = f"call-{uuid.uuid4().hex[:10]}"
    room_name = f"mavis-room-{call_id}"
    caller_phone = req.caller_phone or "Not provided"
    
    livekit_token_info = telephony_gateway.generate_token(
        room_name=room_name,
        participant_identity=caller_phone
    )

    initial_greeting = (
        "Hello, this is MAVIS, the hospital's AI intake assistant. I can collect your details and help request an appointment. "
        "If you have an emergency, call 112 or 108. What is your full name?"
    )

    recorder = CallTranscriptRecorder.get_or_create(call_id, caller_phone)
    recorder.add_turn("mavis", initial_greeting)

    audio_bytes, audio_mime = await tts_engine.synthesize_async(initial_greeting)
    audio_b64 = base64.b64encode(audio_bytes).decode("ascii") if audio_bytes else ""

    await audit_service.log_event(
        db=db,
        event_type="VOICE_CALL_SESSION_STARTED",
        payload_data={
            "call_id": call_id,
            "client_type": req.client_type,
            "caller_phone": caller_phone,
            "room_name": room_name
        },
        call_id=call_id
    )
    await db.commit()

    return {
        "call_id": call_id,
        "room_name": room_name,
        "livekit": livekit_token_info,
        "stage": "name",
        "system_speech": initial_greeting,
        "audio_base64": audio_b64,
        "audio_mime": audio_mime,
        "caller_phone": caller_phone,
        "patient_name": "Patient",
        "patient_age": 35
    }

@router.post("/call/start")
async def start_call_token(body: LiveKitStartCallRequest):
    caller_id = body.caller_id.strip()
    room = f"call-{secrets.token_hex(6)}"
    identity = f"web-{secrets.token_hex(6)}"
    token_info = telephony_gateway.generate_token(room, caller_id)
    return {
        "url": token_info.get("server_url", "ws://127.0.0.1:7880"),
        "token": token_info.get("token", ""),
        "room": room,
    }

@router.post("/process-turn")
async def process_voice_turn(req: ProcessTurnRequest, db: AsyncSession = Depends(get_db)):
    """
    HTTP Turn Processor: non-blocking async speech-to-text, triage, booking, and neural female audio response.
    """
    return await execute_voice_turn(
        call_id=req.call_id,
        caller_phone=req.caller_phone,
        caller_utterance=req.caller_utterance,
        audio_base64=req.audio_base64,
        current_stage=req.current_stage or "name",
        patient_name=req.patient_name or "Patient",
        patient_age=req.patient_age or 35,
        target_department=req.target_department,
        symptom_description=req.symptom_description,
        severity=req.severity or 5,
        duration=req.duration or "Recent",
        db=db
    )

@router.post("/synthesize-tts")
async def synthesize_tts(req: TTSSynthesizeRequest):
    audio_bytes, audio_mime = await tts_engine.synthesize_async(req.text)
    return Response(content=audio_bytes, media_type=audio_mime)

@router.websocket("/ws/{call_id}")
async def voice_websocket_stream(websocket: WebSocket, call_id: str):
    """
    Full-duplex WebSocket bi-directional streaming pipeline:
    - Receives live audio turn chunks (16kHz PCM / WAV base64)
    - Emits real-time VAD telemetry
    - Dispatches non-blocking conversational turns & returns neural female audio
    """
    await websocket.accept()
    logger.info("WebSocket connected for call_id: %s", call_id)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type in ["audio_turn", "turn"]:
                async with AsyncSessionLocal() as session:
                    res = await execute_voice_turn(
                        call_id=call_id,
                        caller_phone=data.get("caller_phone"),
                        caller_utterance=data.get("caller_utterance", ""),
                        audio_base64=data.get("audio_base64"),
                        current_stage=data.get("current_stage", "name"),
                        patient_name=data.get("patient_name", "Patient"),
                        patient_age=int(data.get("patient_age", 35)),
                        target_department=data.get("target_department"),
                        symptom_description=data.get("symptom_description"),
                        severity=int(data.get("severity", 5)),
                        duration=data.get("duration", "Recent"),
                        db=session
                    )
                    await session.commit()
                await websocket.send_json({"type": "turn_response", **res})

            elif msg_type == "audio_frame":
                b64_pcm = data.get("pcm_base64", "")
                if b64_pcm:
                    raw_bytes = base64.b64decode(b64_pcm)
                    is_voice, conf = vad_processor.evaluate_frame(raw_bytes)
                    await websocket.send_json({
                        "type": "vad_event",
                        "is_voice": is_voice,
                        "confidence": conf
                    })

            elif msg_type == "barge_in":
                logger.info("Caller barge-in detected on WebSocket: %s", call_id)
                await websocket.send_json({"type": "barge_in_acknowledged"})

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for call_id: %s", call_id)
    except Exception as e:
        logger.error("WebSocket stream exception: %s", e)
