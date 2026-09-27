"""
AI Service Module for CAP Compliance Checker
Provides LLM-powered analysis, recommendations, and natural language interfaces.
"""

import os
import re
import hashlib
import logging
import json
import time
import threading
from typing import List, Dict, Optional, Any
import requests

logger = logging.getLogger(__name__)


def _clean_env_value(value: Optional[str]) -> Optional[str]:
    """Normalize environment variable values loaded from shell or .env files."""
    if value is None:
        return None

    cleaned = str(value).strip()
    if not cleaned:
        return None

    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
        cleaned = cleaned[1:-1].strip()

    return cleaned or None


def _redact_api_secrets(value: Optional[str]) -> Optional[str]:
    """Redact API key-like secrets before logging error payloads."""
    if value is None:
        return None

    text = str(value)
    return re.sub(r"\b(sk|rk)-(proj-)?[A-Za-z0-9_\-]+\b", lambda match: f"{match.group(1)}-REDACTED", text)


class AIService:
    """Service for integrating AI capabilities into CAP compliance checking."""

    # Class-level rate limiting across all instances
    _last_request_time = 0
    _request_lock = threading.Lock()
    # Configurable via environment variable (default 500ms between requests)
    _min_request_interval = float(os.getenv("AI_REQUEST_INTERVAL", "0.5"))

    def __init__(self):
        # Detect provider based on which API key is set
        gemini_api_key = self._get_env_api_key("GEMINI_API_KEY")
        openai_api_key = self._get_env_api_key("OPENAI_API_KEY")
        anthropic_api_key = self._get_env_api_key("ANTHROPIC_API_KEY")

        if gemini_api_key:
            self.api_provider = "gemini"
            self.api_key = gemini_api_key
        elif openai_api_key:
            self.api_provider = "openai"
            self.api_key = openai_api_key
        elif anthropic_api_key:
            self.api_provider = "anthropic"
            self.api_key = anthropic_api_key
        else:
            self.api_provider = None
            self.api_key = None

        # Set default model based on provider
        if self.api_provider == "gemini":
            self.model = os.getenv("AI_MODEL", "gemini-3.1-flash-lite").strip()
        elif self.api_provider == "openai":
            self.model = os.getenv("AI_MODEL", "gpt-5.5").strip()
        elif self.api_provider == "anthropic":
            self.model = os.getenv("AI_MODEL", "claude-sonnet-4-6").strip()
        else:
            self.model = None

        self.base_url = self._get_base_url()
        # Track last error for caller visibility (e.g., rate limits)
        self.last_error = None
        # Deterministic mode: when true, force temperature=0 and prefer top_p=1
        self.deterministic = str(os.getenv("AI_DETERMINISTIC", "0")).lower() in ("1", "true", "yes")
        # Simple in-memory cache for identical requests
        self._cache: Dict[str, str] = {}

    @staticmethod
    def _get_env_api_key(env_var: str) -> Optional[str]:
        """Read and normalize API keys from environment variables."""
        return _clean_env_value(os.getenv(env_var))

    def _get_base_url(self) -> str:
        """Get the appropriate API base URL."""
        if self.api_provider == "openai":
            return "https://api.openai.com/v1"
        elif self.api_provider == "anthropic":
            return "https://api.anthropic.com/v1"
        elif self.api_provider == "gemini":
            return "https://generativelanguage.googleapis.com/v1beta"
        return None

    def _throttle_request(self):
        """Ensure minimum time between API requests to prevent rate limiting."""
        with AIService._request_lock:
            current_time = time.time()
            time_since_last = current_time - AIService._last_request_time
            if time_since_last < AIService._min_request_interval:
                sleep_time = AIService._min_request_interval - time_since_last
                time.sleep(sleep_time)
            AIService._last_request_time = time.time()

    def _call_llm(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 2000,
        temperature: float = 0.7,
        max_retries: int = 6,
        model: Optional[str] = None,
    ) -> str:
        """Make API call to LLM provider with retry logic for rate limits.

        This implementation adds a simple in-memory cache keyed by a stable SHA256
        of the request parameters so identical inputs can reuse prior responses.
        """

        def _uses_openai_responses_api(m: Optional[str]) -> bool:
            """GPT-5 family models use the Responses API (not Chat Completions)."""
            try:
                if not m:
                    return False
                s = str(m).lower().strip()
                return "gpt-5" in s
            except Exception:
                return False

        def _normalize_model_name(m: Optional[str]) -> Optional[str]:
            if not m:
                return m
            ms = str(m).strip()
            # UI/legacy aliases
            aliases = {
                "gpt-5.4 mini": "gpt-5.4-mini",
                "gpt-5.4-mini": "gpt-5.4-mini",
                "gpt-5.5 mini": "gpt-5.5-mini",
                "gpt-5.5-mini": "gpt-5.5-mini",
            }
            return aliases.get(ms.lower(), ms)

        def _extract_openai_text(resp_json: Dict[str, Any]) -> str:
            # Responses API often provides output_text
            if isinstance(resp_json, dict):
                if isinstance(resp_json.get("output_text"), str) and resp_json.get("output_text"):
                    return resp_json["output_text"]

                # Try to walk output -> content -> text
                output = resp_json.get("output")
                if isinstance(output, list) and output:
                    collected: List[str] = []
                    for item in output:
                        content = item.get("content") if isinstance(item, dict) else None
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and isinstance(c.get("text"), str):
                                    collected.append(c["text"])
                    if collected:
                        return "\n".join(collected)

                    # Some responses may include a reasoning summary rather than text.
                    summaries: List[str] = []
                    for item in output:
                        if isinstance(item, dict) and item.get("type") == "reasoning":
                            summary = item.get("summary")
                            if isinstance(summary, list):
                                for s in summary:
                                    if isinstance(s, str) and s.strip():
                                        summaries.append(s.strip())
                            elif isinstance(summary, str) and summary.strip():
                                summaries.append(summary.strip())
                    if summaries:
                        return "\n".join(summaries)

                # If incomplete due to max_output_tokens and no text is present, return a clean message.
                if resp_json.get("status") == "incomplete":
                    details = resp_json.get("incomplete_details")
                    reason = details.get("reason") if isinstance(details, dict) else None
                    if reason == "max_output_tokens":
                        return "AI response was truncated due to output token limit. Please retry with a higher token limit."

                # Chat Completions format
                choices = resp_json.get("choices")
                if isinstance(choices, list) and choices:
                    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
                    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                        return msg["content"]

            # Never return raw JSON to the UI; it's noisy and not user-friendly.
            return "AI response could not be parsed into text."

        # Use instance default if not provided
        model = _normalize_model_name(model or self.model)

        # Determine provider based on model name if possible, or fallback to instance provider
        provider = self.api_provider
        api_key = self.api_key

        if model:
            if "gemini" in model.lower():
                provider = "gemini"
                api_key = self._get_env_api_key("GEMINI_API_KEY") or self.api_key
            elif "gpt" in model.lower():
                provider = "openai"
                api_key = self._get_env_api_key("OPENAI_API_KEY") or self.api_key
            elif "claude" in model.lower():
                provider = "anthropic"
                api_key = self._get_env_api_key("ANTHROPIC_API_KEY") or self.api_key

        if provider and not api_key:
            self.last_error = f"missing_{provider}_api_key"
            return f"AI analysis unavailable: {provider.title()} API key is not configured."

        # Adjust max_tokens for reasoning models
        if model and (
            "gemini-3-flash-preview" in model
            or "gemini-3.1-pro-preview" in model
            or "gemini-3-pro-preview" in model
        ):
            # Reasoning / preview models need more tokens for internal reasoning traces
            max_tokens = max(max_tokens, 8192)
            # Increase timeout for reasoning models
            timeout = 60
        else:
            timeout = 30

        # OpenAI GPT-5 via Responses API can take longer, especially with higher output token budgets.
        try:
            if provider == "openai" and _uses_openai_responses_api(model):
                timeout = max(timeout, 90)
        except Exception:
            pass

        # Increase max_retries for better rate limit tolerance
        max_retries = 10

        # If deterministic mode is enabled, force deterministic sampling
        if getattr(self, "deterministic", False):
            temperature = 0.0

        # Build a stable cache key for this request
        try:
            messages_serial = json.dumps(messages or [], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            key_raw = f"model={model}|max_tokens={max_tokens}|temp={temperature}|det={self.deterministic}|messages={messages_serial}"
            cache_key = hashlib.sha256(key_raw.encode("utf-8")).hexdigest()
        except Exception:
            cache_key = None

        if cache_key and cache_key in self._cache:
            return self._cache[cache_key]

        for attempt in range(max_retries):
            # Throttle requests to prevent hitting rate limits
            self._throttle_request()

            try:
                if provider == "openai":
                    headers = {
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    }

                    # GPT-5 family often requires the newer Responses API.
                    if _uses_openai_responses_api(model):
                        responses_payload: Dict[str, Any] = {
                            "model": model,
                            "input": [
                                {
                                    "role": m.get("role", "user"),
                                    "content": m.get("content", ""),
                                }
                                for m in (messages or [])
                            ],
                            "max_output_tokens": max_tokens,
                        }

                        response = requests.post(
                            "https://api.openai.com/v1/responses",
                            headers=headers,
                            json=responses_payload,
                            timeout=timeout,
                        )
                        response.raise_for_status()
                        self.last_error = None
                        resp_json = response.json()

                        # If the model spent the entire budget on reasoning and produced no text,
                        # retry once with a higher output token budget.
                        try:
                            if (
                                isinstance(resp_json, dict)
                                and resp_json.get("status") == "incomplete"
                                and isinstance(resp_json.get("incomplete_details"), dict)
                                and resp_json.get("incomplete_details", {}).get("reason") == "max_output_tokens"
                                and max_tokens < 2048
                                and attempt < (max_retries - 1)
                            ):
                                max_tokens = min(max_tokens * 2, 2048)
                                logger.debug(
                                    f"OpenAI response incomplete due to max_output_tokens; retrying with max_output_tokens={max_tokens}"
                                )
                                continue
                        except Exception:
                            pass

                        resp_text = _extract_openai_text(resp_json)
                        if cache_key:
                            self._cache[cache_key] = resp_text
                        return resp_text

                    # Default: Chat Completions API
                    chat_payload = {
                        "model": model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    }
                    # When deterministic, prefer nucleus sampling disabled
                    if getattr(self, "deterministic", False):
                        chat_payload["top_p"] = 1

                    response = requests.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers=headers,
                        json=chat_payload,
                        timeout=timeout,
                    )
                    response.raise_for_status()
                    self.last_error = None
                    resp_text = _extract_openai_text(response.json())
                    if cache_key:
                        self._cache[cache_key] = resp_text
                    return resp_text

                elif provider == "anthropic":
                    response = requests.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={
                            "x-api-key": api_key,
                            "anthropic-version": "2023-06-01",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model,
                            "messages": messages,
                            "max_tokens": max_tokens,
                            "temperature": temperature,
                        },
                        timeout=timeout,
                    )
                    response.raise_for_status()
                    resp_text = response.json()["content"][0]["text"]
                    if cache_key:
                        self._cache[cache_key] = resp_text
                    return resp_text

                elif provider == "gemini":
                    # Convert messages to Gemini format
                    gemini_contents = []
                    for msg in messages:
                        role = "user" if msg["role"] == "user" else "model"
                        gemini_contents.append(
                            {"role": role, "parts": [{"text": msg["content"]}]}
                        )

                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                    gemini_payload = {
                        "contents": gemini_contents,
                        "generationConfig": {
                            "temperature": temperature,
                            "maxOutputTokens": max_tokens,
                        },
                    }

                    response = requests.post(
                        url,
                        headers={
                            "Content-Type": "application/json",
                            "X-goog-api-key": api_key,
                        },
                        json=gemini_payload,
                        timeout=timeout,
                    )
                    response.raise_for_status()
                    # Clear last error on success
                    self.last_error = None
                    response_json = response.json()
                    try:
                        resp_text = response_json["candidates"][0]["content"]["parts"][0]["text"]
                    except (KeyError, IndexError) as e:
                        logger.error(f"Unexpected Gemini response structure: {json.dumps(response_json)}")
                        # Check for safety ratings or other reasons
                        if "candidates" in response_json and response_json["candidates"]:
                            candidate = response_json["candidates"][0]
                            if "finishReason" in candidate:
                                resp_text = f"AI generation stopped. Reason: {candidate['finishReason']}"
                            else:
                                raise e
                    if cache_key:
                        self._cache[cache_key] = resp_text
                    return resp_text

            except requests.exceptions.HTTPError as e:
                # Check if it's a rate limit error (429)
                if e.response.status_code == 429:
                    if attempt < max_retries - 1:
                        # Optimized exponential backoff: starts gentler, scales reasonably
                        import random
                        # Start with 1s, then 2s, 4s, 8s... (capped at 30s)
                        base_wait = min(2 ** attempt, 30)
                        jitter = random.uniform(0, 0.2 * base_wait)
                        wait_time = base_wait + jitter
                        logger.warning(
                            f"Rate limit hit, waiting {wait_time:.1f}s before retry {attempt + 1}/{max_retries}"
                        )
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"Rate limit exceeded after {max_retries} retries")
                        self.last_error = "rate_limit"
                        return "AI analysis temporarily unavailable due to rate limits. Please try again in a minute."
                else:
                    response_status = getattr(e.response, "status_code", None)
                    response_body = None
                    # Log server-provided error details when available (helps diagnose 400s)
                    try:
                        response_body = _redact_api_secrets(getattr(e.response, "text", None))
                        if response_body and len(response_body) > 3000:
                            response_body = response_body[:3000] + "..."
                        logger.error(
                            f"LLM API call failed: {e} | status={response_status} | body={response_body}"
                        )
                    except Exception:
                        logger.error(f"LLM API call failed: {e}")

                    error_code = None
                    try:
                        error_payload = e.response.json()
                        if isinstance(error_payload, dict):
                            error_info = error_payload.get("error")
                            if isinstance(error_info, dict):
                                error_code = error_info.get("code") or error_info.get("type")
                    except Exception:
                        error_payload = None

                    if provider == "openai" and response_status == 401:
                        self.last_error = "invalid_openai_api_key"
                        return "AI analysis unavailable: OpenAI API key is invalid or revoked."

                    self.last_error = error_code or str(e)
                    return f"AI analysis unavailable: {str(e)}"
            except requests.exceptions.Timeout as e:
                if attempt < max_retries - 1:
                    import random
                    base_wait = (2 ** attempt)
                    jitter = random.uniform(0, 0.2 * base_wait)
                    wait_time = base_wait + jitter
                    logger.warning(
                        f"LLM request timed out, waiting {wait_time:.1f}s before retry {attempt + 1}/{max_retries}"
                    )
                    time.sleep(wait_time)
                    continue
                logger.error(f"LLM API call failed: {e}")
                self.last_error = str(e)
                return f"AI analysis unavailable: {str(e)}"
            except Exception as e:
                logger.error(f"LLM API call failed: {e}")
                self.last_error = str(e)
                return f"AI analysis unavailable: {str(e)}"

        return "AI analysis unavailable after multiple retries."

    def generate_smart_recommendations(
        self,
        check_name: str,
        status: str,
        findings: List[str],
        evidence: List[str],
        current_recommendations: List[str],
        model: Optional[str] = None,
    ) -> List[str]:
        """Generate AI-powered, context-aware recommendations based on compliance check results."""

        if not self.api_key and not model:
            return current_recommendations

        prompt = f"""You are a CAP (College of American Pathologists) compliance expert for clinical laboratories.

Compliance Check: {check_name}
Current Status: {status}
Findings: {json.dumps(findings, indent=2)}
Evidence: {json.dumps(evidence, indent=2)}

Current generic recommendations:
{json.dumps(current_recommendations, indent=2)}

Based on the specific findings and evidence from this LIMS, provide 3-5 prioritized, actionable recommendations that:
1. Address the specific gaps found (not generic advice)
2. Reference concrete LIMS data when possible
3. Include step-by-step implementation guidance
4. Prioritize high-impact, achievable actions
5. Cite specific CAP checklist requirements
6. Keep each recommendation short (max 15-20 words)

Format as a JSON array of strings. Be extremely concise."""

        messages = [
            {
                "role": "system",
                "content": "You are a CAP compliance expert specializing in clinical laboratory quality management.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            # Debug logging: Show the AI query being sent for recommendations
            from datetime import datetime
            timestamp = datetime.now().strftime("%H:%M:%S")
            logger.debug(f"\n[{timestamp}] --- AI RECOMMENDATIONS QUERY START ---")
            logger.debug(f"Check: {check_name}")
            logger.debug(f"Status: {status}")
            logger.debug(f"Model: {model or 'default'}")
            logger.debug(f"\n=== Compliance Data ===")
            logger.debug(f"Findings ({len(findings)} items):")
            for i, finding in enumerate(findings[:3]):
                logger.debug(f"  {i+1}. {str(finding)[:100]}")
            if len(findings) > 3:
                logger.debug(f"  ... and {len(findings) - 3} more findings")
            
            logger.debug(f"\nEvidence ({len(evidence)} items):")
            for i, item in enumerate(evidence[:3]):
                logger.debug(f"  {i+1}. {str(item)[:100]}")
            if len(evidence) > 3:
                logger.debug(f"  ... and {len(evidence) - 3} more items")
            
            logger.debug(f"\nCurrent Recommendations: {current_recommendations}")
            logger.debug(f"\n=== AI Prompt ===")
            logger.debug(f"System Role: {messages[0]['content']}")
            logger.debug(f"User Prompt:\n{messages[1]['content']}")
            logger.debug(f"\n--- AI RECOMMENDATIONS QUERY END ---\n")
            
            response = self._call_llm(messages, max_tokens=1500, temperature=0.5, model=model)
            # Try to extract JSON array from response
            if "[" in response and "]" in response:
                start = response.index("[")

                end = response.rindex("]") + 1
                recommendations = json.loads(response[start:end])
                return (
                    recommendations
                    if isinstance(recommendations, list)
                    else current_recommendations
                )
        except Exception as e:
            logger.warning(f"Failed to parse AI recommendations: {e}")

        return current_recommendations

    def _extract_top_evidence(self, evidence: List[str], limit: int = 3) -> List[str]:
        """Extract the top evidence items sorted by AI Match score.
        
        Looks for "(AI Match: XX%)" pattern in evidence items and sorts by score.
        Returns up to `limit` items, preferring those with high AI Match scores.
        """
        import re
        
        scored_items = []
        unscored_items = []
        
        for item in evidence:
            if not item:
                continue
            # Look for (AI Match: XX%) pattern
            match_pattern = r'\(AI Match:\s*(\d+)%\)'
            match = re.search(match_pattern, item)
            
            if match:
                score = int(match.group(1))
                scored_items.append((score, item))
            else:
                unscored_items.append(item)
        
        # Sort scored items by score (highest first)
        scored_items.sort(key=lambda x: x[0], reverse=True)
        
        # Combine: top scored items first, then unscored items
        top_items = [item for score, item in scored_items[:limit]]
        
        # If we have room, add unscored items
        if len(top_items) < limit and unscored_items:
            remaining_slots = limit - len(top_items)
            top_items.extend(unscored_items[:remaining_slots])
        
        return top_items

    def _parse_ai_match(self, item: str) -> int:
        """Return AI Match percentage found in an evidence string, or 0 if none."""
        if not item:
            return 0
        m = re.search(r"\(AI Match:\s*(\d+)%\)", item)
        try:
            if m:
                return int(m.group(1))
        except Exception:
            pass
        return 0

    def summarize_evidence(self, check_name: str, evidence: List[str], actual_status: str = None, model: Optional[str] = None) -> str:
        """Generate a clear, audit-ready summary of compliance evidence."""

        if (not self.api_key and not model) or not evidence:
            return "No evidence available."
        
        # Extract top evidence items sorted by AI Match score
        evidence_strings = [str(e) for e in evidence if e]
        top_evidence = self._extract_top_evidence(evidence_strings, limit=3)
        
        # Map status values to display tokens
        status_token_map = {
            "COMPLIANT": "[COMPLIANT]",
            "NON-COMPLIANT": "[NON-COMPLIANT]",
            "WARNING": "[WARNING]",
            "N/A": "[NOT APPLICABLE]"
        }
        status_token = status_token_map.get(actual_status, "[COMPLIANT]") if actual_status else "[COMPLIANT]"

        # Build prompt with explicit top evidence section and avoid repeating them
        top_evidence_section = "\n    ".join([f"  {i+1}. {item}" for i, item in enumerate(top_evidence)])

        # Create a remaining evidence list excluding top evidence items to avoid duplication
        try:
            remaining_evidence = [e for e in evidence_strings if e not in top_evidence]
        except Exception:
            remaining_evidence = []

        other_section = json.dumps(remaining_evidence, indent=2)

        prompt = f"""Summarize the compliance evidence for CAP check "{check_name}".

    TOP EVIDENCE (highest AI Match scores):
    {top_evidence_section}

    Other Evidence ({len(remaining_evidence)} items):
    {other_section}

    OUTPUT RULES:
    1. REQUIRED: Start with the status token {status_token} followed by a colon. DO NOT change or determine the status - use the provided token exactly.
    2. In 35-50 words total:
       a) State the summary of evidence.
       b) List the top 2-3 matched documents with AI Match scores.
    3. CRITICAL: Always include the COMPLETE document identifier including any numbering system, prefix, or reference number for ALL mentioned documents.
    4. PRIORITY: Lead with the highest AI Match score documents.
    5. Do NOT invent facts—only infer from titles/types.
    6. Keep language audit-ready and factual.
    7. Do NOT use ellipsis (...) or truncation.
    8. Be concise, but ensure all relevant evidence is mentioned. TARGET LENGTH: 35-50 words.
    9. If records are missing but policies are found, explicitly mention the policy documents found.

    Format example:
    [COMPLIANT]: All policies and records present. Top evidence: SKG0106-101-lab Manual EN V3.0 (AI Match: 100%); K1003.8_Annual QMS Assessment.docx (AI Match: 45%)."""

        messages = [
            {
                "role": "system",
                "content": "You are a clinical laboratory quality manager preparing audit documentation. ALWAYS lead with the highest AI-scored documents and include their match percentages. Include complete document identifiers and numbering systems.",
            },
            {"role": "user", "content": prompt},
        ]

        try:            
            # Debug logging: Show the AI query being sent with formatted output
            from datetime import datetime
            timestamp = datetime.now().strftime("%H:%M:%S")
            logger.debug(f"\n[{timestamp}] --- AI SEARCH SUMMARY QUERY START ---")
            logger.debug(f"Check: {check_name}")
            logger.debug(f"Model: {model or 'default'}")
            logger.debug(f"\n=== Top Evidence Items ({len(top_evidence)} selected) ===")
            for i, item in enumerate(top_evidence):
                logger.debug(f"  {i+1}. {str(item)[:150]}")
            logger.debug(f"\n=== Other Evidence Items ({len(remaining_evidence)} total) ===")
            for i, item in enumerate(remaining_evidence[:5]):  # Show first 5
                logger.debug(f"  {i+1}. {str(item)[:120]}")
            if len(remaining_evidence) > 5:
                logger.debug(f"  ... and {len(remaining_evidence) - 5} more items")
            
            logger.debug(f"\n=== AI Prompt ===")
            logger.debug(f"System Role: {messages[0]['content']}")
            logger.debug(f"User Prompt Length: {len(messages[1]['content'])} characters")
            logger.debug(f"User Prompt:\n{messages[1]['content']}")
            logger.debug(f"\n--- AI SEARCH SUMMARY QUERY END ---\n")
            
            # GPT-5 models can consume a lot of output tokens on reasoning;
            # give them a larger budget to ensure we get a complete textual summary.
            # Increased from 300/800 to 2000/4000 to accommodate comprehensive evidence summaries
            # Further increased to ensure NO truncation of evidence summaries with multiple records/policy items
            max_out = 1000 if (model and "gpt-5" in str(model).lower()) else 500
            return self._call_llm(messages, max_tokens=max_out, temperature=0.3, model=model)
        except Exception as e:
            logger.warning(f"Failed to generate evidence summary: {e}")
            return " | ".join(evidence[:3])  # Fallback to simple join

    def analyze_compliance_gaps(
        self, all_check_results: List[Dict[str, Any]], model: Optional[str] = None
    ) -> Dict[str, Any]:
        """Perform intelligent gap analysis across all compliance checks."""

        if not self.api_key and not model:
            return {"analysis": "AI analysis unavailable - no API key configured"}

        # Filter non-compliant and warning checks
        problem_checks = []
        for result in all_check_results:
            if result["status"] not in ["NON_COMPLIANT", "WARNING"]:
                continue

            problem_checks.append(
                {
                    "check": result["check"],
                    "requirement": (result.get("requirement") or "").strip(),
                    "status": result["status"],
                    "findings": (result.get("findings") or [])[:3],
                    "ai_evidence_summary": (result.get("ai_evidence_summary") or "").strip(),
                    "details": (result.get("details") or "").strip(),
                }
            )

        # Canonicalize ordering to ensure deterministic prompts
        try:
            problem_checks.sort(key=lambda x: x.get("check", ""))
        except Exception:
            pass

        if not problem_checks:
            return {
                "analysis": "✓ All compliance checks passed. No gaps identified.",
                "priority_actions": [],
                "risk_level": "LOW",
            }

        prompt = f"""Analyze the following CAP compliance gaps and provide prioritized remediation guidance.

Critical instructions:
- Use the exact requirement text provided for each check.
- Do not substitute a different CAP checklist topic, clause, or laboratory process.
- If the evidence only says records were not found, describe the gap as missing evidence for that specific requirement rather than inventing a different deficiency.
- Recommendations must stay tightly tied to the stated requirement and the provided findings/evidence summary.

Non-Compliant/Warning Checks:
{json.dumps(problem_checks, indent=2)}

Provide a gap analysis in JSON format:
{{
    "analysis": "2-3 sentence overview that explicitly references the provided requirement topic",
    "priority_actions": [
        {{"action": "description", "impact": "HIGH|MEDIUM|LOW", "timeframe": "IMMEDIATE|30_DAYS|90_DAYS", "rationale": "why this matters"}}
    ],
    "risk_level": "HIGH|MEDIUM|LOW",
    "regulatory_impact": "brief explanation of audit/accreditation risks"
}}"""

        messages = [
            {
                "role": "system",
                "content": "You are a CAP compliance consultant performing gap analysis for clinical laboratories.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            # Debug logging: Show the gap analysis query
            from datetime import datetime
            timestamp = datetime.now().strftime("%H:%M:%S")
            logger.debug(f"\n[{timestamp}] --- AI GAP ANALYSIS QUERY START ---")
            logger.debug(f"Model: {model or 'default'}")
            logger.debug(f"\n=== Compliance Status ===")
            logger.debug(f"Total Checks: {len(all_check_results)}")
            compliant_count = sum(1 for r in all_check_results if r.get('status') == 'COMPLIANT')
            non_compliant_count = sum(1 for r in all_check_results if r.get('status') == 'NON_COMPLIANT')
            warning_count = sum(1 for r in all_check_results if r.get('status') == 'WARNING')
            logger.debug(f"  - Compliant: {compliant_count}")
            logger.debug(f"  - Non-Compliant: {non_compliant_count}")
            logger.debug(f"  - Warnings: {warning_count}")
            
            logger.debug(f"\n=== Problem Checks ({len(problem_checks)} total) ===")
            for i, check in enumerate(problem_checks[:5]):
                logger.debug(
                    f"  {i+1}. {check.get('check', 'Unknown')} [{check.get('status', 'UNKNOWN')}]"
                    f" requirement={check.get('requirement', '')[:120]}"
                )
            if len(problem_checks) > 5:
                logger.debug(f"  ... and {len(problem_checks) - 5} more")
            
            logger.debug(f"\n=== AI Prompt ===")
            logger.debug(f"System Role: {messages[0]['content']}")
            logger.debug(f"User Prompt:\n{messages[1]['content']}")
            logger.debug(f"\n--- AI GAP ANALYSIS QUERY END ---\n")
            
            response = self._call_llm(messages, max_tokens=1500, temperature=0.4, model=model)
            # Extract JSON from response
            if "{" in response and "}" in response:
                start = response.index("{")
                end = response.rindex("}") + 1
                return json.loads(response[start:end])
        except Exception as e:
            logger.warning(f"Failed to parse gap analysis: {e}")

        return {
            "analysis": f"Found {len(problem_checks)} compliance gaps requiring attention.",
            "priority_actions": [],
            "risk_level": "MEDIUM",
        }

    _GAP_ASSESSMENT_EVIDENCE_LIMIT = 8
    _GAP_ASSESSMENT_MAX_WORDS = 200

    @staticmethod
    def _format_gap_assessment_evidence(evidence: List[Any], limit: int = 8) -> List[str]:
        formatted = []
        for item in evidence or []:
            if isinstance(item, dict):
                name = (
                    item.get("document_name")
                    or item.get("title")
                    or item.get("name")
                    or ""
                )
                snippet = str(item.get("snippet") or "").strip()
                ai_match = item.get("ai_match") or item.get("relevance_score")
                match_suffix = ""
                if ai_match is not None:
                    try:
                        pct = int(float(ai_match) * 100) if float(ai_match) <= 1 else int(ai_match)
                        match_suffix = f" (AI Match: {pct}%)"
                    except (TypeError, ValueError):
                        pass
                if name and snippet:
                    formatted.append(f"{name}{match_suffix}: {snippet[:200]}")
                elif name:
                    formatted.append(f"{name}{match_suffix}")
            elif item:
                formatted.append(str(item))
            if len(formatted) >= limit:
                break
        return formatted

    @staticmethod
    def _normalize_workbook_gap_status(status_value: Any) -> str:
        status = str(status_value or "").strip().lower()
        if status in ("compliant", "comply", "pass", "ok"):
            return "Compliant"
        if status in ("partial", "warning", "caution"):
            return "Partial"
        if "non" in status and "compliant" in status:
            return "Non-Compliant"
        if status in ("n/a", "na", "not applicable"):
            return "N/A"
        return str(status_value or "Partial").strip() or "Partial"

    @staticmethod
    def _action_completion_indicates_open_work(action_completion: str) -> bool:
        text = str(action_completion or "").strip().lower()
        if not text:
            return False
        open_markers = (
            "[not started",
            "[in progress",
            "in progress",
            "pending",
            "waiting for",
            "not complete",
            "not completed",
            "not yet",
            "still need",
            "still required",
            "to do",
            "todo",
            "open gap",
            "remains open",
        )
        if any(marker in text for marker in open_markers):
            complete_markers = (
                "[complete",
                "[verified",
                "completed",
                "complete.",
                "resolved",
                "implemented",
                "remediated",
                "closed out",
                "addressed",
                "gaps closed",
                "gap closed",
            )
            if not any(marker in text for marker in complete_markers):
                return True
        return False

    _REMEDIATION_STRONG_MARKERS = (
        "[complete",
        "[verified",
        "completed on",
        "completed.",
        "completion verified",
        "corrective action complete",
        "corrective actions complete",
        "all corrective actions",
        "gaps closed",
        "gap closed",
        "remediation complete",
        "resolved",
        "implemented",
        "closed out",
        "addressed",
    )
    _REMEDIATION_COMPLETION_WORDS = (
        "complete",
        "completed",
        "verified",
        "resolved",
        "implemented",
        "remediated",
        "posted",
        "installed",
        "displayed",
        "uploaded",
        "filed",
        "oriented",
        "trained",
        "developed",
        "added",
        "updated",
        "in place",
        "on site",
        "on-site",
    )
    _REMEDIATION_CATEGORY_TOKENS = {
        "document": ("policy", "sop", "procedure", "document", "manual"),
        "records": ("record", "records", "lims", "form", "log", "worksheet", "report"),
        "onsite": (
            "onsite",
            "on-site",
            "on site",
            "site visit",
            "walkthrough",
            "physical inspection",
            "posted",
            "sign",
            "lab",
            "laboratory",
            "prominent",
            "displayed",
            "installed",
            "visible",
            "location",
            "verified",
            "confirmed",
            "correctly",
        ),
    }
    _REMEDIATION_KEYWORD_STOP_WORDS = {
        "continue",
        "current",
        "practice",
        "verify",
        "records",
        "policy",
        "corrective",
        "action",
        "actions",
        "inspection",
        "packet",
        "document",
        "filed",
        "obtain",
        "locate",
        "ensure",
        "develop",
        "implement",
        "formal",
    }

    @classmethod
    def _verification_keyword_in_text(cls, text_lower: str, keyword: str) -> bool:
        if keyword in text_lower:
            return True
        if len(keyword) < 4:
            return False
        for word in re.findall(r"\b[a-z]+\b", text_lower):
            if word.startswith(keyword) or keyword.startswith(word):
                return True
        return False

    @classmethod
    def _verification_notes_from_ctx(cls, requirement_ctx: Dict[str, Any]) -> List[str]:
        notes: List[str] = []
        for key in ("remediation", "action_completion", "onsite_note"):
            text = str(requirement_ctx.get(key) or "").strip()
            if text and text not in notes:
                notes.append(text)
        return notes

    @classmethod
    def _best_verification_resolution_level(
        cls,
        verification_notes: List[str],
        missing_evidence: Optional[List[Any]] = None,
        corrective_actions: Optional[List[Any]] = None,
    ) -> Optional[str]:
        rank = {"Partial": 1, "Compliant": 2}
        best: Optional[str] = None
        for note in verification_notes:
            level = cls._remediation_resolution_level(
                note, missing_evidence, corrective_actions
            )
            if level and (not best or rank[level] > rank[best]):
                best = level
        return best

    @classmethod
    def _remediation_action_keywords(cls, action_text: str, limit: int = 8) -> List[str]:
        keywords = re.findall(r"\b[a-z]{4,}\b", str(action_text or "").lower())
        seen = set()
        ordered: List[str] = []
        for word in keywords:
            if word in cls._REMEDIATION_KEYWORD_STOP_WORDS or word in seen:
                continue
            seen.add(word)
            ordered.append(word)
            if len(ordered) >= limit:
                break
        return ordered

    @classmethod
    def _remediation_addresses_action_item(cls, remediation_lower: str, action_item: str) -> bool:
        keywords = cls._remediation_action_keywords(action_item)
        if not keywords:
            return False
        matches = sum(
            1 for word in keywords
            if cls._verification_keyword_in_text(remediation_lower, word)
        )
        required = 1 if len(keywords) <= 2 else min(2, len(keywords))
        return matches >= required

    @classmethod
    def _remediation_covers_missing_evidence(
        cls,
        remediation_lower: str,
        missing_evidence: Optional[List[Any]] = None,
    ) -> bool:
        missing = missing_evidence or []
        if isinstance(missing, str):
            missing = [missing]
        missing = [str(item).strip().lower() for item in missing if str(item).strip()]
        if not missing:
            return True
        for category in missing:
            tokens = cls._REMEDIATION_CATEGORY_TOKENS.get(
                category, (category.replace("_", " "),)
            )
            if not any(token in remediation_lower for token in tokens):
                return False
        return True

    @classmethod
    def _remediation_has_completion_language(cls, remediation_lower: str) -> bool:
        if any(marker in remediation_lower for marker in cls._REMEDIATION_STRONG_MARKERS):
            return True
        return any(word in remediation_lower for word in cls._REMEDIATION_COMPLETION_WORDS)

    @classmethod
    def _remediation_resolution_level(
        cls,
        action_completion: str,
        missing_evidence: Optional[List[Any]] = None,
        corrective_actions: Optional[List[Any]] = None,
    ) -> Optional[str]:
        """Return Compliant, Partial, or None based on remediation text."""
        text = str(action_completion or "").strip()
        if not text or cls._action_completion_indicates_open_work(text):
            return None

        lower = text.lower()
        if any(marker in lower for marker in cls._REMEDIATION_STRONG_MARKERS):
            return "Compliant"

        if not cls._remediation_has_completion_language(lower):
            return None

        if not cls._remediation_covers_missing_evidence(lower, missing_evidence):
            return None

        actions = corrective_actions or []
        if isinstance(actions, str):
            actions = [actions]
        action_items = [str(item).strip() for item in actions if str(item).strip()]
        if not action_items:
            return "Compliant"

        addressed = [
            item for item in action_items
            if cls._remediation_addresses_action_item(lower, item)
        ]
        if not addressed:
            keywords = cls._remediation_action_keywords(" ".join(action_items))
            if keywords and not any(
                cls._verification_keyword_in_text(lower, word) for word in keywords
            ):
                return None
            return "Partial"

        if len(addressed) >= len(action_items):
            return "Compliant"
        return "Partial"

    @classmethod
    def _action_completion_satisfies_gaps(
        cls,
        action_completion: str,
        missing_evidence: Optional[List[Any]] = None,
        corrective_actions: Optional[List[Any]] = None,
    ) -> bool:
        return cls._remediation_resolution_level(
            action_completion, missing_evidence, corrective_actions
        ) == "Compliant"

    @classmethod
    def _format_verification_gap_note(cls, requirement_ctx: Dict[str, Any]) -> str:
        parts: List[str] = []
        remediation = str(
            requirement_ctx.get("remediation")
            or requirement_ctx.get("action_completion")
            or ""
        ).strip()
        onsite_note = str(requirement_ctx.get("onsite_note") or "").strip()
        if remediation:
            parts.append(f"Remediation: {remediation}")
        if onsite_note:
            parts.append(f"Onsite Note: {onsite_note}")
        return " ".join(parts)

    @classmethod
    def _apply_action_completion_to_gap_assessment(
        cls,
        assessment: Dict[str, Any],
        requirement_ctx: Dict[str, Any],
    ) -> Dict[str, Any]:
        verification_notes = cls._verification_notes_from_ctx(requirement_ctx)
        if not verification_notes:
            return assessment

        status = cls._normalize_workbook_gap_status(assessment.get("status"))
        if status in ("Compliant", "N/A"):
            return assessment

        missing = requirement_ctx.get("missing_evidence") or []
        corrective_actions = assessment.get("corrective_actions") or requirement_ctx.get("corrective_actions") or []
        resolution = cls._best_verification_resolution_level(
            verification_notes, missing, corrective_actions
        )
        if not resolution:
            return assessment

        current_rank = {"Non-Compliant": 0, "Partial": 1, "Compliant": 2}.get(status, 0)
        new_rank = {"Partial": 1, "Compliant": 2}.get(resolution, 0)
        if new_rank <= current_rank:
            return assessment

        updated = dict(assessment)
        updated["status"] = resolution
        gap_text = str(updated.get("gap_analysis") or "").strip()
        note = cls._format_verification_gap_note(requirement_ctx)
        if gap_text.lower().startswith("status:"):
            updated["gap_analysis"] = f"Status: {resolution}. {note}"
        elif gap_text:
            updated["gap_analysis"] = (
                f"Status: {resolution}. Prior assessment: {gap_text} {note}"
            )
        else:
            updated["gap_analysis"] = f"Status: {resolution}. {note}"
        return updated

    @staticmethod
    def _fallback_cap_gap_assessment(requirement_ctx: Dict[str, Any]) -> Dict[str, Any]:
        req_id = str(requirement_ctx.get("requirement_id") or "").strip()
        missing = requirement_ctx.get("missing_evidence") or []
        if isinstance(missing, str):
            missing = [missing]
        evidence = requirement_ctx.get("retrieved_documents") or []
        if missing and evidence:
            status = "Partial"
            gap = (
                f"Status: Partial. Some evidence located for {req_id}; "
                f"not verified: {', '.join(str(m) for m in missing)}."
            )
            actions = [
                f"1) Locate and verify {', '.join(str(m) for m in missing)} evidence.",
                "2) File verified records in the inspection packet.",
            ]
        elif evidence:
            status = "Compliant"
            gap = f"Status: Compliant. Evidence documents located for {req_id}."
            actions = ["Continue current practice. Retain cited documents in the inspection packet."]
        elif str(requirement_ctx.get("note") or "").lower().find("not applicable") >= 0:
            status = "N/A"
            gap = f"Status: N/A. Requirement appears not applicable based on CAP note."
            actions = ["Document N/A. Record rationale in the inspection workbook."]
        else:
            status = "Non-Compliant"
            gap = f"Status: Non-Compliant. Required evidence not verified for {req_id}."
            actions = [
                f"1) Locate documentation supporting {req_id}.",
                "2) Verify records against Evidence of Compliance criteria.",
            ]
        result = {
            "requirement_id": req_id,
            "status": status,
            "gap_analysis": gap,
            "corrective_actions": actions,
            "cited_documents": [],
        }
        return AIService._apply_action_completion_to_gap_assessment(result, requirement_ctx)

    def generate_cap_requirement_gap_assessment(
        self,
        requirement_ctx: Dict[str, Any],
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate CAP workbook-style gap analysis and corrective actions for one requirement."""
        req_id = str(
            requirement_ctx.get("requirement_id")
            or requirement_ctx.get("check")
            or requirement_ctx.get("checklist_item")
            or ""
        ).strip()

        if not self.api_key and not model:
            return self._fallback_cap_gap_assessment(requirement_ctx)

        findings = requirement_ctx.get("findings") or []
        if isinstance(findings, str):
            findings = [findings]
        missing = requirement_ctx.get("missing_evidence") or []
        if isinstance(missing, str):
            missing = [missing]

        evidence_items = (
            requirement_ctx.get("evidence_items")
            or requirement_ctx.get("retrieved_documents")
            or requirement_ctx.get("results")
            or requirement_ctx.get("policy_evidence")
            or []
        )

        payload = {
            "requirement_id": req_id,
            "subject_header": (requirement_ctx.get("subject") or "").strip(),
            "requirement": (requirement_ctx.get("requirement") or "").strip(),
            "note": (requirement_ctx.get("note") or requirement_ctx.get("cap_note") or "").strip(),
            "policy_procedure": (requirement_ctx.get("policy_procedure") or "").strip(),
            "evidence_of_compliance": (
                requirement_ctx.get("evidence_of_compliance") or ""
            ).strip(),
            "missing_evidence_categories": list(missing),
            "findings": [str(item).strip() for item in findings if str(item).strip()],
            "retrieved_documents": self._format_gap_assessment_evidence(
                evidence_items, limit=self._GAP_ASSESSMENT_EVIDENCE_LIMIT
            ),
            "search_summary": (
                requirement_ctx.get("ai_evidence_summary")
                or requirement_ctx.get("ai_search_summary")
                or ""
            ).strip(),
            "action_completion": (
                str(
                    requirement_ctx.get("remediation")
                    or requirement_ctx.get("action_completion")
                    or ""
                ).strip()
            ),
            "onsite_note": str(requirement_ctx.get("onsite_note") or "").strip(),
            "corrective_actions_existing": [
                str(item).strip()
                for item in (
                    requirement_ctx.get("corrective_actions")
                    if isinstance(requirement_ctx.get("corrective_actions"), list)
                    else []
                )
                if str(item).strip()
            ],
        }

        temperature = 0.0 if self.deterministic else 0.2
        max_words = self._GAP_ASSESSMENT_MAX_WORDS

        prompt = f"""Generate a CAP inspection workbook row for this checklist requirement.

Checklist context (authoritative requirement framing):
{json.dumps(payload, indent=2)}

Gap Analysis rules:
1. gap_analysis MUST start with exactly one of: "Status: Compliant.", "Status: Partial.", "Status: Non-Compliant.", "Status: N/A."
2. Write up to {max_words} words in gap_analysis.
3. Cite exact document IDs/names ONLY from retrieved_documents or search_summary — never invent document numbers.
4. For Partial: state what evidence IS present and what was NOT verified (especially records vs policy).
5. If records were not found, say "not verified" rather than inventing record IDs.
6. Use Evidence of Compliance criteria and Note to judge N/A and Partial outcomes.

Remediation and Onsite Note rules:
1. When remediation (Remediation column) or onsite_note (Onsite Note column) is non-empty, decide whether identified gaps are remediated or onsite requirements are verified.
2. Onsite Note is authoritative for physical/onsite verification (e.g. sign posted, walkthrough confirmed) even when retrieved_documents lack photographic proof.
3. If remediation or onsite_note credibly documents completed corrective work (e.g. [COMPLETE], [VERIFIED], posted, verified, confirmed), status MUST reflect that even if search_summary alone was Partial or Non-Compliant.
4. Use the best-supported outcome across remediation and onsite_note; onsite verification can satisfy onsite missing_evidence categories.
5. If either note indicates work is still open ([IN PROGRESS], pending, not started), keep Partial or Non-Compliant.
6. When remediation or onsite_note changes the status, state that in gap_analysis and reference the note briefly.

Corrective Actions rules:
1. Return 1-3 corrective_actions strings.
2. Compliant → "Continue current practice. [specific maintenance tied to cited documents]."
3. Partial or Non-Compliant → numbered steps "1) ..." "2) ..." (max 3).
4. N/A → "Document N/A. [one-line rationale from Note or lab context]."
5. Do not assign owners unless explicitly included in the action text.

Return JSON only:
{{
  "requirement_id": "{req_id}",
  "status": "Compliant|Partial|Non-Compliant|N/A",
  "gap_analysis": "...",
  "corrective_actions": ["...", "..."],
  "cited_documents": ["..."]
}}"""

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a CAP compliance consultant preparing inspection gap analysis workbooks. "
                    "Be audit-ready, specific, and grounded only in provided checklist and evidence data."
                ),
            },
            {"role": "user", "content": prompt},
        ]

        try:
            max_out = 1200 if (model and "gpt-5" in str(model).lower()) else 900
            response = self._call_llm(
                messages, max_tokens=max_out, temperature=temperature, model=model
            )
            if "{" in response and "}" in response:
                start = response.index("{")
                end = response.rindex("}") + 1
                parsed = json.loads(response[start:end])
                if isinstance(parsed, dict):
                    actions = parsed.get("corrective_actions") or []
                    if isinstance(actions, str):
                        actions = [actions]
                    parsed_result = {
                        "requirement_id": req_id or parsed.get("requirement_id"),
                        "status": str(parsed.get("status") or "Partial").strip(),
                        "gap_analysis": str(parsed.get("gap_analysis") or "").strip(),
                        "corrective_actions": [
                            str(item).strip() for item in actions if str(item).strip()
                        ],
                        "cited_documents": parsed.get("cited_documents") or [],
                    }
                    return self._apply_action_completion_to_gap_assessment(
                        parsed_result, requirement_ctx
                    )
        except Exception as exc:
            logger.warning("CAP gap assessment failed for %s: %s", req_id, exc)

        return self._fallback_cap_gap_assessment(requirement_ctx)

    def natural_language_query(
        self,
        query: str,
        all_check_results: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
        model: Optional[str] = None,
    ) -> str:
        """Answer natural language questions about compliance status."""

        if not self.api_key and not model:
            return "AI query feature unavailable - no API key configured."

        # Normalize status values to a canonical set so counting is reliable.
        def _normalize_status(s: Any) -> str:
            if s is None:
                return "UNKNOWN"
            st = str(s).strip()
            # Common enum .value for NOT_APPLICABLE uses "N/A"; normalize to NOT_APPLICABLE
            if st in ["N/A", "NA", "Not Applicable", "Not applicable", "N A"]:
                return "NOT_APPLICABLE"
            # Normalize common variants
            if st.upper() in ["COMPLIANT", "COMPLY", "PASS", "OK"]:
                return "COMPLIANT"
            if st.upper() in [
                "NON_COMPLIANT",
                "NON-COMPLIANT",
                "NON COMPLIANT",
                "FAIL",
            ]:
                return "NON_COMPLIANT"
            if st.upper() in ["WARNING", "WARN"]:
                return "WARNING"
            if st.upper() == "NOT_APPLICABLE":
                return "NOT_APPLICABLE"
            return st.upper()

        normalized = []
        for r in all_check_results:
            ns = _normalize_status(r.get("status"))
            normalized.append(
                {
                    "check": r.get("check"),
                    "status": ns,
                    "findings": r.get("findings", []),
                    "subject": r.get("subject", "") or r.get("Subject", ""),
                    "policy": r.get("policy", "")
                    or r.get("Policy/Procedure", "")
                    or r.get("Policy", ""),
                    "ai_prompt": r.get("ai_prompt", ""),
                    "app_keywords": r.get("app_keywords", ""),
                    "evidence": r.get("evidence", []),
                }
            )

        # Sort normalized entries by check name for deterministic prompts
        try:
            normalized.sort(key=lambda x: (x.get("check") or ""))
        except Exception:
            pass

        # Canonicalize evidence within each check
        for entry in normalized:
            try:
                ev = entry.get("evidence") or []
                entry["evidence"] = sorted(ev, key=lambda s: (-self._parse_ai_match(s), s or ""))
            except Exception:
                pass

        # Prepare context summary using normalized statuses
        compliance_summary = {
            "total_checks": len(normalized),
            "compliant": sum(1 for r in normalized if r["status"] == "COMPLIANT"),
            "warnings": sum(1 for r in normalized if r["status"] == "WARNING"),
            "non_compliant": sum(
                1 for r in normalized if r["status"] == "NON_COMPLIANT"
            ),
            # Treat UNKNOWN as equivalent to NOT_APPLICABLE for summary counts
            "not_applicable": sum(
                1 for r in normalized if r["status"] in ("NOT_APPLICABLE", "UNKNOWN")
            ),
            "checks": [
                {
                    "name": r["check"],
                    "status": r["status"],
                    "findings": r["findings"][:2],  # Limit findings
                    "evidence": r["evidence"], # Include evidence list for context
                    "subject": r.get("subject", ""),
                    "policy": r.get("policy", ""),
                    "instructions": r.get("ai_prompt", ""),
                    "keywords": r.get("app_keywords", ""),
                }
                for r in normalized
            ],
        }

        prompt = f"""You are a CAP compliance assistant for a clinical laboratory LIMS.

Current Compliance Status:
{json.dumps(compliance_summary, indent=2)}

Additional Context:
{json.dumps(context or {}, indent=2)}

User Question: {query}

Provide a clear, accurate answer based on the compliance data. Be specific and cite check names when relevant.
If the question cannot be answered from the data, say so."""

        messages = [
            {
                "role": "system",
                "content": "You are a helpful CAP compliance assistant with deep knowledge of laboratory quality standards.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            # Debug logging: Show the full formatted AI Query
            from datetime import datetime
            timestamp = datetime.now().strftime("%H:%M:%S")
            logger.debug(f"\n[{timestamp}] --- AI QUERY START ---")
            logger.debug(f"Query Type: Natural Language Compliance Query")
            logger.debug(f"Model: {model or 'default'}")
            logger.debug(f"User Question: {query}")
            logger.debug(f"\n=== Compliance Summary ===")
            logger.debug(f"Total Checks: {compliance_summary.get('total_checks', 0)}")
            logger.debug(f"  - Compliant: {compliance_summary.get('compliant', 0)}")
            logger.debug(f"  - Non-Compliant: {compliance_summary.get('non_compliant', 0)}")
            logger.debug(f"  - Warnings: {compliance_summary.get('warnings', 0)}")
            logger.debug(f"  - Not Applicable: {compliance_summary.get('not_applicable', 0)}")
            logger.debug(f"\n=== Check Details ===")
            for check in compliance_summary.get("checks", [])[:10]:  # Limit to first 10
                logger.debug(f"  • {check.get('name', 'Unknown')} [{check.get('status', 'UNKNOWN')}]")
                if check.get('findings'):
                    logger.debug(f"    Findings: {check.get('findings', [])[:1]}")
            if len(compliance_summary.get("checks", [])) > 10:
                logger.debug(f"  ... and {len(compliance_summary.get('checks', [])) - 10} more checks")
            
            if context:
                logger.debug(f"\n=== Additional Context ===")
                for key, value in context.items():
                    logger.debug(f"  {key}: {str(value)[:100]}...")
            
            logger.debug(f"\n=== Full Prompt Sent to AI ===")
            logger.debug(f"System Message: {messages[0]['content'][:100]}...")
            logger.debug(f"User Prompt Length: {len(messages[1]['content'])} characters")
            logger.debug(f"User Prompt:\n{messages[1]['content']}")
            logger.debug(f"\n--- AI QUERY END ---\n")
            
            return self._call_llm(messages, max_tokens=800, temperature=0.6, model=model)
        except Exception as e:
            logger.warning(f"Failed to process natural language query: {e}")
            return f"Unable to process query: {str(e)}"

    def predict_compliance_trends(
        self,
        historical_results: List[Dict[str, Any]],
        current_results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Analyze trends and predict potential future compliance issues."""

        if not self.api_key or not historical_results:
            return {"prediction": "Insufficient historical data for trend analysis"}

        # Prepare trend data
        trend_summary = {
            "historical_checks": len(historical_results),
            "current_checks": len(current_results),
            "improving": [],
            "degrading": [],
            "stable": [],
        }

        # Simple trend detection
        hist_by_check = {r["check"]: r["status"] for r in historical_results}
        curr_by_check = {r["check"]: r["status"] for r in current_results}

        for check_name in curr_by_check:
            if check_name in hist_by_check:
                if hist_by_check[check_name] != curr_by_check[check_name]:
                    if curr_by_check[check_name] == "COMPLIANT":
                        trend_summary["improving"].append(check_name)
                    elif curr_by_check[check_name] == "NON_COMPLIANT":
                        trend_summary["degrading"].append(check_name)
                else:
                    trend_summary["stable"].append(check_name)

        prompt = f"""Analyze compliance trends and predict potential issues.

Trend Summary:
{json.dumps(trend_summary, indent=2)}

Provide predictions in JSON format:
{{
    "trend_analysis": "brief overview of trends",
    "at_risk_areas": ["area1", "area2"],
    "predicted_issues": [
        {{"area": "name", "likelihood": "HIGH|MEDIUM|LOW", "timeframe": "description", "mitigation": "action"}}
    ],
    "recommendations": ["proactive recommendation"]
}}"""

        messages = [
            {
                "role": "system",
                "content": "You are a predictive analytics expert for laboratory compliance management.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = self._call_llm(messages, max_tokens=1200, temperature=0.5)
            if "{" in response and "}" in response:
                start = response.index("{")
                end = response.rindex("}") + 1
                return json.loads(response[start:end])
        except Exception as e:
            logger.warning(f"Failed to generate trend predictions: {e}")

        return {"prediction": "Trend analysis unavailable"}

    def classify_documents(self, filenames: List[str], model: Optional[str] = None) -> Dict[str, str]:
        """Classify documents as Policy, SOP, Record, etc. based on filename."""
        if (not self.api_key and not model) or not filenames:
            return {}
            
        # Limit to 800 filenames to avoid context limits
        filenames_subset = filenames[:800]
        
        prompt = f"""Classify the following document filenames into one of these types: 
- Policy (includes policies, manuals, plans)
- SOP (Standard Operating Procedures, protocols)
- Record (forms, logs, checklists, training records, quizzes, raw data, reports, worksheets)
- Validation (validation reports, verification)
- Other (if unclear)

Filenames:
{json.dumps(filenames_subset, indent=2)}

Return a JSON object mapping filename to type:
{{"filename1.pdf": "Policy", "filename2.docx": "Record", ...}}

Only return the JSON object."""

        messages = [
            {
                "role": "system",
                "content": "You are a document control specialist for a clinical laboratory.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = self._call_llm(messages, max_tokens=4000, temperature=0.1, model=model)
            # Extract JSON
            if "{" in response and "}" in response:
                start = response.index("{")
                end = response.rindex("}") + 1
                return json.loads(response[start:end])
        except Exception as e:
            logger.warning(f"Failed to classify documents: {e}")
            
        return {}

    def find_relevant_documents(
        self, query: str, document_list: List[Dict[str, Any]], model: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Use AI to find relevant SOPs, policies, and validation documents."""

        if (not self.api_key and not model) or not document_list:
            return []

        # Prepare document metadata
        # Include optional lightweight content snippet when available to improve grounding.
        doc_metadata = []
        for i, doc in enumerate(document_list[:300]):
            item = {
                "title": doc.get("Title", ""),
                "doc_num": doc.get("Document #", ""),
                "type": doc.get("Type", ""),
                "index": i,
            }
            if doc.get("ContentSnippet"):
                # Keep snippet reasonably small to control tokens
                snip = str(doc.get("ContentSnippet") or "")
                if len(snip) > 1500:
                    snip = snip[:1500]
                item["content_snippet"] = snip
            doc_metadata.append(item)

        prompt = f"""You are ranking policy/procedure evidence.
    Task: Given the query, select the most relevant documents.

    Important:
    - Prefer Policies, SOPs, Procedures, Manuals, and Validation documents over generic records when both match.
    - Use content_snippet when present; it contains partial text from the document.
    - Score strictly by relevance to the query, considering titles, doc numbers, types, and snippets.

    Query:
    {query}

    Available Documents (truncated):
    {json.dumps(doc_metadata, indent=2)}

    Return a JSON array of objects for the top 5 most relevant documents, with index and a relevance score (0-100):
    [{{"index": 0, "score": 95}}, {{"index": 5, "score": 80}}]

    Only return the JSON array, no explanation."""

        messages = [
            {
                "role": "system",
                "content": "You are a document retrieval expert for clinical laboratory quality systems. Evaluate document relevance strictly.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = self._call_llm(messages, max_tokens=1000, temperature=0.3, model=model)
            # Extract array
            if "[" in response and "]" in response:
                start = response.index("[")
                end = response.rindex("]") + 1
                results = json.loads(response[start:end])
                
                final_docs = []
                for item in results:
                    if isinstance(item, dict):
                        idx = item.get("index")
                        score = item.get("score")
                        if idx is not None and isinstance(idx, int) and 0 <= idx < len(document_list):
                            doc = document_list[idx].copy()
                            if score:
                                doc["relevance_score"] = score
                            final_docs.append(doc)
                    elif isinstance(item, int): # Fallback for old integer-only array format
                         if 0 <= item < len(document_list):
                             final_docs.append(document_list[item])
                
                return final_docs
        except Exception as e:
            logger.warning(f"Failed to find relevant documents: {e}")

        return []


# Global AI service instance
ai_service = AIService()
