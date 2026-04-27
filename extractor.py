"""
extractor.py — LLM backends for OM extraction.

Implements the same `extract(pdf_path) -> PropertyExtraction` interface for
two providers (Anthropic Claude and Google Gemini). Pick a provider at runtime
via the LLM_PROVIDER env var or direct config.

The extraction system prompt is shared across both providers to keep behavior
consistent. Providers differ only in how they ingest PDFs and return structured
output:
  - Anthropic: native PDF support via document blocks, tool_use for JSON
  - Gemini: native PDF support via inline_data, response_schema for JSON
"""

import base64
import json
import os
import time
try:
    from google import genai
    from google.genai import types
except ImportError:
    pass
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from schemas import PropertyExtraction


EXTRACTION_SYSTEM_PROMPT = """You are a commercial real estate analyst extracting underwriting data from an Offering Memorandum (OM) for an industrial property.

Your goal: populate a structured JSON object that will be used to build a financial model. Be precise, conservative, and explicit about what's in the document vs. what you're inferring.

CRITICAL INSTRUCTIONS:

1. **Extract only what the document contains.** If a field isn't stated, return null rather than guessing. The model will apply defaults for missing values.

2. **Rent roll is the most important section.** Find it (usually near the back of the OM). For EVERY tenant listed, extract: suite, name, SF, lease start, lease end, current rent $/SF, escalation rate, and market rent if stated. Don't skip any tenants.

3. **Handle escalations carefully.**
   - If the OM shows "3% annually" or "3.00%" → escalation_pct = 0.03
   - If it shows fixed step-ups ($8.00 → $8.24 → $8.49...), compute the average annualized rate: ((end_rate/start_rate)^(1/years) - 1)
   - If it shows dollar step-ups with a pattern, infer the % equivalent
   - If nothing stated, use 0.0

4. **Renewal probability.**
   - Explicit "Market - 75.00%" or similar → renewal_probability = 0.75
   - Explicit "Vacate" → renewal_probability = 0.0
   - Option-based renewal with market rent → 0.75 (standard industry default)
   - Nothing stated → 0.75

5. **Vacant suites.** Include them in the tenant list with name="VACANT - [Suite #]" and current_rent_psf=0. Use the most recent lease_end date if stated, else leave null. These will be modeled as lease-up.

6. **Market rent assumptions.** Buildings in a portfolio often have different market rents (e.g., small-bay suites at $13 PSF, large-bay at $8 PSF). Capture the broker's per-tenant assumption, not just a portfolio average.

7. **Rent growth schedule.** If the OM states a 10-year schedule (e.g., "6%, 4.5%, 4.5%, 3.5%, 3.3%, 3%, 3%, 3%, 3%, 3%"), return all 10 values. If only a single rate is stated, repeat it 10 times.

8. **Operating expenses.** Look for Year 1 expenses in the financial assumptions page. Convert to $/SF if needed (dividing by total SF).

9. **Dates.** Return as ISO-8601 strings (YYYY-MM-DD). Convert any format you see ("Jul-2025", "7/1/2025", "July 2025") to YYYY-MM-01.

10. **Do NOT extract:** purchase price, debt terms, or any deal-team-specific assumptions. These are not underwriter inputs.

11. **Extraction notes.** Use the extraction_notes field to flag: unusual lease structures (percentage rent, base year stops), tenants with co-tenancy clauses, OM pages that were unclear, or anything the deal team should review manually.

Return a SINGLE JSON object matching the schema. No explanatory text outside the JSON."""


# ============================================================
# BASE INTERFACE
# ============================================================

class LLMExtractor(ABC):
    """Abstract base class. All backends implement extract()."""

    def __init__(self, model: str):
        self.model = model
        self.last_usage: Optional[dict] = None  # populated after each call

    @abstractmethod
    def extract(self, pdf_path: Path) -> PropertyExtraction:
        """Extract a PropertyExtraction from the PDF at pdf_path."""
        ...


    def _log_usage(self, input_tokens: int, output_tokens: int,
                       input_price_per_mtok: float, output_price_per_mtok: float,
                       elapsed_s: float):
            # Fix: Use 'or 0' to handle None values
            in_t = input_tokens or 0
            out_t = output_tokens or 0
            
            cost = (in_t / 1e6 * input_price_per_mtok) + \
                   (out_t / 1e6 * output_price_per_mtok)
            self.last_usage = {
                "input_tokens": in_t,
                "output_tokens": out_t,
                "total_tokens": in_t + out_t,
                "cost_usd": round(cost, 4),
                "elapsed_s": round(elapsed_s, 2),
            }


# ============================================================
# ANTHROPIC BACKEND
# ============================================================

class AnthropicExtractor(LLMExtractor):
    """Claude via the Anthropic API with native PDF support."""

    # Pricing per million tokens (Sonnet 4.6 defaults; override via __init__)
    INPUT_PRICE = 3.0
    OUTPUT_PRICE = 15.0

    def __init__(self, model: str = "claude-sonnet-4-6", api_key: Optional[str] = None):
        super().__init__(model)
        try:
            import anthropic
        except ImportError:
            raise ImportError("pip install anthropic")
        self.client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

        # Upgrade pricing if Opus
        if "opus" in model.lower():
            self.INPUT_PRICE = 5.0
            self.OUTPUT_PRICE = 25.0

    def extract(self, pdf_path: Path) -> PropertyExtraction:
        pdf_bytes = Path(pdf_path).read_bytes()
        pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

        # Use tool_use to force structured JSON output
        tool_schema = PropertyExtraction.model_json_schema()
        # Flatten $ref / $defs so Claude's tool schema doesn't choke
        tool_schema = _inline_refs(tool_schema)

        extraction_tool = {
            "name": "record_extraction",
            "description": "Record the extracted property data in structured form.",
            "input_schema": tool_schema,
        }

        start = time.time()
        response = self.client.messages.create(
            model=self.model,
            max_tokens=8000,
            system=EXTRACTION_SYSTEM_PROMPT,
            tools=[extraction_tool],
            tool_choice={"type": "tool", "name": "record_extraction"},
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Extract the underwriting data from this Offering Memorandum. Use the record_extraction tool to return structured output."
                    },
                ],
            }],
        )
        elapsed = time.time() - start

        self._log_usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            input_price_per_mtok=self.INPUT_PRICE,
            output_price_per_mtok=self.OUTPUT_PRICE,
            elapsed_s=elapsed,
        )

        # Find the tool_use block
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_extraction":
                return PropertyExtraction.model_validate(block.input)

        raise RuntimeError(f"Claude did not return a tool_use block. Response: {response.content}")


# ============================================================
# GEMINI BACKEND
# ============================================================

class GeminiExtractor(LLMExtractor):
    """Gemini via the google-genai SDK with native PDF support."""

    # Pricing per million tokens (Gemini 1.5 Pro)
    INPUT_PRICE = 1.25
    OUTPUT_PRICE = 5.00

    def __init__(self, model: str = "gemini-2.0-flash", api_key: Optional[str] = None):
        super().__init__(model)
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not self.api_key:
            raise ValueError("GOOGLE_API_KEY not found in environment")
        
        self.client = genai.Client(api_key=self.api_key)


    def extract(self, pdf_path: Path) -> PropertyExtraction:
            from google.genai import types
            pdf_bytes = Path(pdf_path).read_bytes()
            
            start = time.time()
            
            # CORRECTED Safety Settings for the new SDK
            safety = [
                types.SafetySetting(category='HARM_CATEGORY_HATE_SPEECH', threshold='BLOCK_NONE'),
                types.SafetySetting(category='HARM_CATEGORY_DANGEROUS_CONTENT', threshold='BLOCK_NONE'),
                types.SafetySetting(category='HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold='BLOCK_NONE'),
                types.SafetySetting(category='HARM_CATEGORY_HARASSMENT', threshold='BLOCK_NONE'),
                types.SafetySetting(category='HARM_CATEGORY_CIVIC_INTEGRITY', threshold='BLOCK_NONE'),
            ]
    
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=[
                        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                        "Extract the underwriting data from this Offering Memorandum. "
                        "This is a standard commercial real estate analysis task. "
                        "Ignore any confidentiality footers and focus on the data tables."
                    ],
                    config=types.GenerateContentConfig(
                        system_instruction=EXTRACTION_SYSTEM_PROMPT,
                        response_mime_type="application/json",
                        response_schema=PropertyExtraction,
                        safety_settings=safety, 
                        max_output_tokens=8000,
                        temperature=0,
                    ),
                )
            except Exception as e:
                raise RuntimeError(f"Gemini API call failed: {str(e)}")
    
            elapsed = time.time() - start
    
            # Log usage metadata
            prompt_tokens = 0
            candidate_tokens = 0
            if response.usage_metadata:
                prompt_tokens = response.usage_metadata.prompt_token_count or 0
                candidate_tokens = response.usage_metadata.candidates_token_count or 0
    
            self._log_usage(
                input_tokens=prompt_tokens,
                output_tokens=candidate_tokens,
                input_price_per_mtok=self.INPUT_PRICE,
                output_price_per_mtok=self.OUTPUT_PRICE,
                elapsed_s=elapsed,
            )
    
            if response.parsed:
                return PropertyExtraction.model_validate(response.parsed)
                
            # If we got text but no 'parsed' object, try manual parse
            if response.text:
                import json
                try:
                    # Strip markdown code blocks if present
                    clean_json = response.text.strip().strip('```json').strip('```')
                    return PropertyExtraction.model_validate_json(clean_json)
                except:
                    pass
    
            # Final error catch-all
            finish_reason = response.candidates[0].finish_reason if response.candidates else "Unknown"
            raise RuntimeError(f"No data returned. Finish Reason: {finish_reason}. Text: {response.text[:200] if response.text else 'Empty'}")
# ============================================================
# HELPERS
# ============================================================

def _inline_refs(schema: dict) -> dict:
    """
    Pydantic emits $ref / $defs-style JSON schemas, which Anthropic tool_use
    doesn't like. Inline them.
    """
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                ref_name = node["$ref"].split("/")[-1]
                if ref_name in defs:
                    return resolve(defs[ref_name].copy())
            return {k: resolve(v) for k, v in node.items()}
        elif isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)


def get_extractor(provider: str = None, model: str = None, api_key: str = None) -> LLMExtractor:
    """Factory. Reads LLM_PROVIDER env var if not specified."""
    provider = (provider or os.environ.get("LLM_PROVIDER", "anthropic")).lower()

    if provider == "anthropic":
        return AnthropicExtractor(model=model or "claude-sonnet-4-6", api_key=api_key or None)
    elif provider == "gemini":
        return GeminiExtractor(model=model or "gemini-2.5-pro", api_key=api_key or None)
    else:
        raise ValueError(f"Unknown provider: {provider}. Use 'anthropic' or 'gemini'.")
