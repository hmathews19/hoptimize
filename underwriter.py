"""
underwriter.py — End-to-end underwriting pipeline.

Usage:
    python underwriter.py <input_pdf> [options]

Options:
    --template PATH     Path to Excel template (default: Industrial_UW_Model_v2.xlsx)
    --output-dir PATH   Where to write outputs (default: ./output)
    --provider NAME     'anthropic' or 'gemini' (default: anthropic, or env LLM_PROVIDER)
    --model NAME        Specific model (default: claude-sonnet-4-6)
    --no-recalc         Skip formula recalculation step

Environment variables:
    ANTHROPIC_API_KEY   Required for Anthropic backend
    GOOGLE_API_KEY      Required for Gemini backend
    LLM_PROVIDER        Alternative to --provider flag
"""

import argparse
import json
import sys
import time
import subprocess
from pathlib import Path

from extractor import get_extractor
from populator import populate_template
from report import generate_report


def run_pipeline(
    pdf_path: Path,
    template_path: Path,
    output_dir: Path,
    provider: str = None,
    model: str = None,
    recalc: bool = True,
) -> dict:
    """
    Run the full pipeline. Returns a dict of outputs:
      {
        "model_path": Path,
        "report_path": Path,
        "extraction": PropertyExtraction,
        "usage": dict,
      }
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = pdf_path.stem

    model_output = output_dir / f"{stem}_UW_Model.xlsx"
    report_output = output_dir / f"{stem}_Extraction_Report.md"

    # ---- Step 1: Extract ----
    print(f"\n[1/4] Extracting from {pdf_path.name}...", flush=True)
    extractor = get_extractor(provider=provider, model=model)
    print(f"      Provider: {extractor.__class__.__name__}, Model: {extractor.model}", flush=True)

    t0 = time.time()
    extraction = extractor.extract(pdf_path)
    t1 = time.time()
    print(f"      ✓ Extraction complete in {t1-t0:.1f}s", flush=True)
    if extractor.last_usage:
        u = extractor.last_usage
        print(f"      Usage: {u['input_tokens']:,} in + {u['output_tokens']:,} out tokens, "
              f"${u['cost_usd']:.3f}", flush=True)
    print(f"      Property: {extraction.property_info.name}", flush=True)
    print(f"      Tenants extracted: {len(extraction.tenants)}", flush=True)

    # ---- Step 2: Populate template ----
    print(f"\n[2/4] Populating template...", flush=True)
    population_result = populate_template(
        extraction=extraction,
        template_path=template_path,
        output_path=model_output,
    )
    print(f"      ✓ Wrote {model_output}", flush=True)
    print(f"      {len(population_result.extracted_fields)} fields from OM, "
          f"{len(population_result.defaulted_fields)} defaulted", flush=True)
    for warning in population_result.warnings:
        print(f"      ⚠ {warning}", flush=True)

    # ---- Step 3: Recalculate formulas ----
    if recalc:
        print(f"\n[3/4] Recalculating formulas...", flush=True)
        recalc_script = Path("/mnt/skills/public/xlsx/scripts/recalc.py")
        if recalc_script.exists():
            try:
                result = subprocess.run(
                    ["python3", str(recalc_script), str(model_output), "120"],
                    capture_output=True, text=True, timeout=180,
                )
                if result.returncode == 0:
                    try:
                        status = json.loads(result.stdout)
                        print(f"      ✓ {status.get('total_formulas', '?')} formulas recalculated, "
                              f"{status.get('total_errors', 0)} errors", flush=True)
                    except json.JSONDecodeError:
                        print(f"      ✓ Recalc complete", flush=True)
                else:
                    print(f"      ⚠ Recalc had issues: {result.stderr[:200]}", flush=True)
            except subprocess.TimeoutExpired:
                print(f"      ⚠ Recalc timed out — open the file in Excel to recalculate", flush=True)
        else:
            print(f"      (skipped — recalc script not available on this system)", flush=True)
            print(f"      Open the file in Excel to recalculate formulas", flush=True)
    else:
        print(f"\n[3/4] Skipping recalc (per --no-recalc)", flush=True)

    # ---- Step 4: Generate report ----
    print(f"\n[4/4] Generating extraction report...", flush=True)
    generate_report(
        extraction=extraction,
        population_result=population_result,
        pdf_name=pdf_path.name,
        usage=extractor.last_usage,
        output_path=report_output,
    )
    print(f"      ✓ Wrote {report_output}", flush=True)

    print(f"\n{'='*60}", flush=True)
    print(f"DONE.", flush=True)
    print(f"  Model:  {model_output}", flush=True)
    print(f"  Report: {report_output}", flush=True)
    print(f"{'='*60}\n", flush=True)

    return {
        "model_path": model_output,
        "report_path": report_output,
        "extraction": extraction,
        "usage": extractor.last_usage,
    }


def main():
    parser = argparse.ArgumentParser(description="Underwrite an industrial OM.")
    parser.add_argument("pdf", type=Path, help="Path to the Offering Memorandum PDF")
    parser.add_argument("--template", type=Path,
                        default=Path(__file__).parent / "Industrial_UW_Model_v2.xlsx",
                        help="Path to the Excel template")
    parser.add_argument("--output-dir", type=Path, default=Path("./output"),
                        help="Directory for outputs")
    parser.add_argument("--provider", choices=["anthropic", "gemini"], default=None,
                        help="LLM provider (default: anthropic)")
    parser.add_argument("--model", default=None,
                        help="Specific model name (default: claude-sonnet-4-6)")
    parser.add_argument("--no-recalc", action="store_true",
                        help="Skip formula recalculation")
    args = parser.parse_args()

    if not args.pdf.exists():
        print(f"Error: PDF not found: {args.pdf}", file=sys.stderr)
        sys.exit(1)
    if not args.template.exists():
        print(f"Error: Template not found: {args.template}", file=sys.stderr)
        sys.exit(1)

    try:
        run_pipeline(
            pdf_path=args.pdf,
            template_path=args.template,
            output_dir=args.output_dir,
            provider=args.provider,
            model=args.model,
            recalc=not args.no_recalc,
        )
    except Exception as e:
        print(f"\nERROR: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
