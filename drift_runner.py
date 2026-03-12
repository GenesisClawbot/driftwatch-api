"""
Drift detection runner - executes drift checks for user prompts
Reuses logic from /workspace/llm-drift/core/drift_detector.py
"""
import os
import re
import json
import time
from typing import Dict, List, Optional
import anthropic


def validate_response(response: str, validators: List[str]) -> Dict[str, bool]:
    """Run validators against an LLM response (matches drift_detector.py logic)"""
    results = {}
    text = response.strip()
    
    for v in validators:
        if v == "is_valid_json":
            try:
                json.loads(text)
                results[v] = True
            except json.JSONDecodeError:
                results[v] = False
        elif v == "is_json_array":
            try:
                parsed = json.loads(text)
                results[v] = isinstance(parsed, list)
            except:
                results[v] = False
        elif v.startswith("has_keys:"):
            keys = v.split(":")[1].split(",")
            try:
                parsed = json.loads(text)
                results[v] = all(k in str(parsed) for k in keys)
            except:
                results[v] = False
        elif v == "single_word":
            results[v] = len(text.split()) == 1
        elif v.startswith("word_in:"):
            options = v.split(":")[1].split(",")
            results[v] = text.strip().lower() in [o.lower() for o in options]
        elif v.startswith("max_words:"):
            limit = int(v.split(":")[1])
            results[v] = len(text.split()) <= limit
        elif v == "response_length_min:100":
            results[v] = len(text) >= 100
        elif v == "no_refusal":
            refusal_signals = ["i can't", "i cannot", "i'm unable", "i won't"]
            results[v] = not any(s in text.lower() for s in refusal_signals)
        else:
            results[v] = True  # Unknown validator passes
    
    return results


def compute_drift_score(baseline_resp: str, current_resp: str,
                       baseline_vals: Dict[str, bool], current_vals: Dict[str, bool]) -> Dict:
    """Compute drift score (matches drift_detector.py logic)"""
    scores = {}
    
    # Validator compliance drift
    if baseline_vals and current_vals:
        baseline_pass = sum(1 for v in baseline_vals.values() if v) / max(len(baseline_vals), 1)
        current_pass = sum(1 for v in current_vals.values() if v) / max(len(current_vals), 1)
        val_drift = abs(baseline_pass - current_pass)
        scores["validator_drift"] = round(val_drift, 3)
        
        regressions = []
        for key, was_pass in baseline_vals.items():
            if was_pass and key in current_vals and not current_vals[key]:
                regressions.append(key)
        scores["regressions"] = regressions
    else:
        scores["validator_drift"] = 0.0
        scores["regressions"] = []
    
    # Length drift
    bl_len = len(baseline_resp.strip())
    cu_len = len(current_resp.strip())
    if bl_len > 0:
        len_ratio = abs(bl_len - cu_len) / bl_len
        scores["length_drift"] = round(min(len_ratio, 1.0), 3)
    else:
        scores["length_drift"] = 0.0
    
    # Word similarity (Jaccard)
    bl_words = set(baseline_resp.lower().split())
    cu_words = set(current_resp.lower().split())
    if bl_words | cu_words:
        overlap = len(bl_words & cu_words) / len(bl_words | cu_words)
        scores["word_similarity"] = round(overlap, 3)
    else:
        scores["word_similarity"] = 1.0
    
    # Overall drift
    val_weight, len_weight, word_weight = 0.5, 0.2, 0.3
    overall = (
        scores["validator_drift"] * val_weight +
        scores["length_drift"] * len_weight +
        (1 - scores["word_similarity"]) * word_weight
    )
    scores["overall_drift"] = round(overall, 3)
    
    # Alert level
    if scores["regressions"]:
        scores["alert_level"] = "critical"
    elif overall >= 0.6:
        scores["alert_level"] = "high"
    elif overall >= 0.3:
        scores["alert_level"] = "medium"
    elif overall >= 0.1:
        scores["alert_level"] = "low"
    else:
        scores["alert_level"] = "none"
    
    return scores


def run_drift_check(prompts: List[Dict], user_api_key: str, model: str = "claude-3-haiku-20240307") -> Dict:
    """
    Run a drift check for a user's prompts
    
    Args:
        prompts: List of prompt dicts with keys: prompt_text, validators, baseline_response
        user_api_key: User's Anthropic API key (or use default from env if not provided)
        model: Model to test
    
    Returns:
        Dict with results, summary stats, and any alerts
    """
    client = anthropic.Anthropic(api_key=user_api_key or os.environ.get("ANTHROPIC_API_KEY"))
    results = []
    
    for prompt in prompts:
        try:
            # Call the LLM
            msg = client.messages.create(
                model=model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt["prompt_text"]}]
            )
            current_response = msg.content[0].text if msg.content else ""
            
            # Validate and compute drift
            baseline_resp = prompt.get("baseline_response", "")
            baseline_vals = prompt.get("baseline_validators", {})
            
            current_vals = validate_response(current_response, prompt.get("validators", []))
            drift = compute_drift_score(baseline_resp, current_response, baseline_vals, current_vals)
            
            results.append({
                "prompt_id": prompt.get("prompt_id"),
                "drift_score": drift["overall_drift"],
                "alert_level": drift["alert_level"],
                "regressions": drift["regressions"],
                "baseline_response": baseline_resp,
                "current_response": current_response,
                "validators": current_vals
            })
            
            time.sleep(0.3)  # Rate limiting
        except Exception as e:
            results.append({
                "prompt_id": prompt.get("prompt_id"),
                "error": str(e),
                "drift_score": None,
                "alert_level": "error"
            })
    
    # Summary statistics
    valid_results = [r for r in results if "error" not in r]
    drifts = [r["drift_score"] for r in valid_results if r["drift_score"] is not None]
    alerts = [r for r in valid_results if r["alert_level"] in ("high", "critical")]
    
    summary = {
        "total_prompts": len(valid_results),
        "avg_drift": round(sum(drifts) / len(drifts), 3) if drifts else 0,
        "max_drift": round(max(drifts), 3) if drifts else 0,
        "alerts": len(alerts),
        "alert_details": alerts
    }
    
    return {
        "results": results,
        "summary": summary
    }
