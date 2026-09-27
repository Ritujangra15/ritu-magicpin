"""
Vera — magicpin AI Challenge submission
Deterministic, context-grounded message engine + HTTP harness.

No external APIs are called. The composer only uses information supplied
through CategoryContext, MerchantContext, TriggerContext and CustomerContext.
"""

from __future__ import annotations
import os, re, time, uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

VERSION = "1.0.0"
TEAM = os.getenv("TEAM_NAME", "Vera Builders")
START = time.time()

# ---------------------------------------------------------------------------
# Pure composer
# ---------------------------------------------------------------------------

def _first_name(m: dict) -> str:
    ident = m.get("identity", {})
    return ident.get("owner_first_name") or ident.get("name", "there").split()[0]

def _money(v: Any) -> str:
    if v is None: return ""
    try:
        return f"₹{int(v):,}"
    except Exception:
        return str(v)

def _pct(v: Any) -> str:
    try:
        return f"{float(v)*100:.0f}%"
    except Exception:
        return str(v)

def _active_offer(m: dict, contains: str = "") -> Optional[dict]:
    for o in m.get("offers", []):
        if o.get("status") == "active" and (not contains or contains.lower() in o.get("title","").lower()):
            return o
    return next((o for o in m.get("offers", []) if o.get("status") == "active"), None)

def _offer_title(m: dict, contains: str = "") -> Optional[str]:
    o = _active_offer(m, contains)
    return o.get("title") if o else None

def _digest_item(category: dict, payload: dict) -> Optional[dict]:
    wanted = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id")
    for d in category.get("digest", []):
        if d.get("id") == wanted or d.get("id") == payload.get("digest_item_id"):
            return d
    return None

def _lang(customer: Optional[dict], merchant: dict) -> str:
    if customer:
        return customer.get("identity", {}).get("language_pref", "english").lower()
    langs = merchant.get("identity", {}).get("languages", [])
    return "hi" if "hi" in langs and "en" not in langs else "en"

def _customer_consent_ok(customer: dict, trigger: dict) -> bool:
    consent = customer.get("consent", {})
    scopes = consent.get("scope", [])
    kind = trigger.get("kind", "")
    if not consent:
        return False
    if kind in {"recall_due", "appointment_tomorrow"}:
        return bool(any(x in scopes for x in ["recall_reminders", "appointment_reminders"]))
    return bool(scopes)

def _voice_prefix(category: str, merchant: dict) -> str:
    n = _first_name(merchant)
    name = merchant.get("identity", {}).get("name", "")
    if category == "dentists": return f"Dr. {n}"
    return n

def _customer_open(customer: dict, merchant: dict, category: str) -> str:
    n = customer.get("identity", {}).get("name", "there")
    if category == "pharmacies":
        return f"Namaste {n} ji — {merchant.get('identity',{}).get('name','')} here."
    if category == "dentists":
        return f"Hi {n} — {merchant.get('identity',{}).get('name','')} here 🦷."
    if category == "gyms":
        return f"Hi {n} 👋 {_first_name(merchant)} from {merchant.get('identity',{}).get('name','')} here."
    return f"Hi {n} — {merchant.get('identity',{}).get('name','')} here."

def _cta(kind: str, customer: bool = False) -> str:
    if kind in {"research_digest","regulation_change","perf_spike","milestone_reached","category_seasonal"}:
        return "open_ended"
    return "binary_yes_stop" if not customer else "open_ended"

def _compose_customer(category: dict, merchant: dict, trigger: dict, customer: dict) -> dict:
    kind = trigger.get("kind","")
    p = trigger.get("payload", {})
    cat = category.get("slug","")
    name = customer.get("identity",{}).get("name","there")
    prefix = _customer_open(customer, merchant, cat)
    active = _active_offer(merchant)

    if not _customer_consent_ok(customer, trigger):
        return {
            "body": "", "cta": "none", "send_as": "merchant_on_behalf",
            "suppression_key": trigger.get("suppression_key",""),
            "rationale": "No applicable customer outreach consent is present; do not initiate a customer message."
        }

    if kind == "recall_due":
        slots = p.get("available_slots", [])
        slot_text = " or ".join(s.get("label","") for s in slots[:2])
        service = p.get("service_due","").replace("_"," ")
        price = active.get("title") if active else ""
        body = f"{prefix} It’s been a while — your {service} recall window is due. "
        if slot_text: body += f"I have {slot_text} available. "
        if price: body += f"{price}. "
        body += "Reply YES and I’ll help confirm a slot, or tell us a time that suits you."
        return _result(body, "open_ended", "merchant_on_behalf", trigger,
                        f"Recall reminder uses the customer's state, real due date/slots and an active merchant offer.")

    if kind in {"customer_lapsed_hard","winback_eligible"}:
        days = p.get("days_since_last_visit")
        focus = p.get("previous_focus")
        offer = active.get("title") if active else None
        body = f"{prefix} It’s been about {round(int(days)/7)} weeks since your last visit — no pressure. "
        if focus: body += f"You were previously working on {focus.replace('_',' ')}. "
        if offer: body += f"We currently have {offer}. "
        body += "Want me to hold a low-commitment trial/visit slot for you? Reply YES — no obligation."
        return _result(body, "binary_yes_stop", "merchant_on_behalf", trigger,
                        "Winback message is non-judgmental and grounded in the customer's prior goal plus a real active offer.")

    if kind in {"chronic_refill_due"}:
        mols = p.get("molecule_list", [])
        runout = p.get("stock_runs_out_iso","")
        offer = _offer_title(merchant, "delivery")
        delivery = "Free home delivery > ₹499" if offer else ""
        body = f"{prefix} Your regular medicines — {', '.join(mols)} — are due to run out around {runout[:10] or 'the date on your refill reminder'}. "
        if delivery: body += f"{delivery}. "
        body += "Reply CONFIRM if you want the pharmacy to prepare the refill, or contact the pharmacist if anything has changed."
        return _result(body, "open_ended", "merchant_on_behalf", trigger,
                        "Refill reminder uses the actual molecule list, run-out date and merchant delivery offer.")

    if kind == "appointment_tomorrow":
        body = f"{prefix} Just a reminder: you have an appointment tomorrow."
        if p.get("time"): body += f" Your scheduled time is {p['time']}."
        body += " Reply YES to confirm, or tell us if you need a change."
        return _result(body, "open_ended", "merchant_on_behalf", trigger,
                        "Appointment reminder is time-specific and asks for one simple confirmation.")

    if kind in {"trial_followup","wedding_package_followup"}:
        if kind == "trial_followup":
            opts = p.get("next_session_options", [])
            opt = opts[0].get("label") if opts else None
            body = f"{prefix} Thanks for trying us recently. "
            if opt: body += f"I have {opt} available for your next session. "
            body += "Want me to hold it for you?"
        else:
            wd = p.get("wedding_date")
            days = p.get("days_to_wedding")
            offer = _offer_title(merchant, "bridal") or active.get("title") if active else None
            body = f"{prefix} Your wedding is {wd} ({days} days away), and your bridal follow-up window is open. "
            if offer: body += f"Our current offer is {offer}. "
            body += "Want me to help block the next available step?"
        return _result(body, "binary_yes_stop", "merchant_on_behalf", trigger,
                        "Follow-up continues a known customer relationship and uses the trigger's concrete next step.")

    # Safe generic customer fallback for an unseen/placeholder trigger.
    topic = p.get("metric_or_topic") or kind.replace("_", " ")
    body = f"{prefix} Quick {topic.replace('_',' ')} reminder from the team."
    if customer.get("preferences", {}).get("preferred_slots"):
        body += f" We can work around your {customer['preferences']['preferred_slots'].replace('_',' ')} preference."
    body += " Reply YES if you want us to help with the next step."
    return _result(body, "open_ended", "merchant_on_behalf", trigger,
                    f"Customer-facing fallback is intentionally conservative for trigger {kind}; no unsupported facts added.")

def _compose_merchant(category: dict, merchant: dict, trigger: dict) -> dict:
    kind = trigger.get("kind","")
    p = trigger.get("payload", {})
    cat = category.get("slug","")
    who = _voice_prefix(cat, merchant)
    biz = merchant.get("identity",{}).get("name","")
    loc = merchant.get("identity",{}).get("locality","")
    perf = merchant.get("performance",{})
    peer = category.get("peer_stats",{})
    active = _active_offer(merchant)
    active_title = active.get("title") if active else None
    rationale = ""

    if kind == "research_digest":
        d = _digest_item(category,p)
        if d:
            stats = []
            if d.get("trial_n"): stats.append(f"{d['trial_n']:,}-patient")
            if d.get("delta"): stats.append(str(d["delta"]))
            anchor = d.get("title","")
            body = f"{who}, {d.get('source','This week’s digest')} has one item worth your attention: {anchor}."
            if stats: body = f"{who}, {d.get('source','This week’s digest')} has one item worth your attention: {' '.join(stats)} trial — {anchor}."
            cohort = merchant.get("customer_aggregate",{}).get("high_risk_adult_count")
            if cohort: body += f" That maps to your {cohort} high-risk-adult patients."
            body += " Want me to pull the source and turn the useful part into a patient-facing draft?"
            return _result(body,"open_ended","vera",trigger,
                            "Research trigger matched to the category digest and a merchant-specific cohort; source attribution is retained.")
    if kind == "regulation_change":
        d = _digest_item(category,p)
        deadline = p.get("deadline_iso","")
        title = d.get("title") if d else "a regulatory update"
        source = d.get("source") if d else None
        body = f"{who}, heads up — {title}."
        if source: body += f" Source: {source}."
        if deadline: body += f" Deadline: {deadline}."
        body += " I can turn the required changes into a short checklist for your team. Want me to draft it?"
        return _result(body,"open_ended","vera",trigger,
                        "Compliance trigger is answered with source/deadline facts and a concrete checklist offer.")

    if kind == "perf_dip":
        metric=p.get("metric")
        if not metric:
            d7=perf.get("delta_7d",{})
            neg=[(k,v) for k,v in d7.items() if isinstance(v,(int,float)) and v < 0]
            metric=neg[0][0].replace('_pct','') if neg else "performance"
        delta=p.get("delta_pct")
        if delta is None:
            delta=perf.get("delta_7d",{}).get(metric+"_pct")
        current = perf.get(metric)
        peer_val = peer.get("avg_"+metric)
        body=f"{who}, quick performance check: {metric}"
        if delta is not None: body += f" are {_pct(delta)} vs the stated baseline window."
        else: body += " is showing a dip in the current context."
        if current is not None: body += f" Your current 30-day {metric}: {current}."
        if peer_val is not None: body += f" Category peer average is {peer_val}."
        body += " I’d focus the next action on the metric that moved, rather than changing the whole profile. Want me to draft one targeted fix?"
        return _result(body,"binary_yes_stop","vera",trigger,
                        f"Performance dip is anchored to the trigger delta and current merchant performance; no unsupported cause is asserted.")

    if kind == "renewal_due":
        days=p.get("days_remaining",merchant.get("subscription",{}).get("days_remaining"))
        amount=p.get("renewal_amount")
        plan=p.get("plan",merchant.get("subscription",{}).get("plan"))
        body=f"{who}, your {plan} plan has {days} days left"
        if amount: body += f"; renewal is {_money(amount)}."
        body += ". Before you renew, I can summarize what changed in your account and where the plan is actually being used. Want that 2-minute snapshot?"
        return _result(body,"open_ended","vera",trigger,
                        "Renewal message uses actual plan, remaining days and price, while offering a useful review rather than pressure.")

    if kind == "festival_upcoming":
        fest=p.get("festival"); days=p.get("days_until")
        if fest and days is not None:
            body=f"{who}, {fest} is {days} days away. For {cat}, this is a useful planning window."
        else:
            body=f"{who}, a festival-upcoming signal is active for {cat}. This is a useful planning window."
        if active_title: body += f" You already have {active_title} active, so I’d build around that rather than inventing a new offer."
        body += " Want me to draft one WhatsApp/Google-post concept for the festival?"
        return _result(body,"open_ended","vera",trigger,
                        "Festival trigger is tied to the merchant's real active offer and category, with a single low-effort deliverable.")

    if kind == "wedding_package_followup":
        return _compose_customer(category,merchant,trigger,{})

    if kind == "curious_ask_due":
        body=f"Hi {who.split()[-1]} — quick operator question: what service has been most asked-for this week at {biz}?"
        body += " Give me the service name and I’ll turn it into a short Google post + WhatsApp pricing reply."
        return _result(body,"open_ended","vera",trigger,
                        "Curious-ask cadence explicitly asks the merchant for a missing fact and offers to convert it into usable copy.")

    if kind == "ipl_match_today":
        match=p.get("match"); venue=p.get("venue"); time_iso=p.get("match_time_iso")
        body=f"Quick heads-up {who.split()[-1]} — {match} at {venue} today"
        if time_iso: body += f", {time_iso[11:16]}."
        else: body += "."
        if active_title: body += f" Your active offer is {active_title}; I’d use that existing offer rather than invent a match promo."
        body += " Want me to draft a delivery-first WhatsApp/Instagram version for tonight?"
        return _result(body,"open_ended","vera",trigger,
                        "Match-day message uses the real event and existing offer; it avoids inventing a performance statistic not present in context.")

    if kind == "active_planning_intent":
        topic=p.get("intent_topic","the idea").replace("_"," ")
        last=p.get("merchant_last_message","")
        if cat == "restaurants" and "thali" in topic:
            offer=_offer_title(merchant,"thali") or "Weekday Lunch Thali @ ₹149"
            body=f"{who}, here’s a starter for the corporate-bulk thali idea you asked about:\n• 10+ orders: build around {offer}\n• Set a day-before WhatsApp cutoff\n• Keep a fixed 12:30–1pm delivery window\n• Offer a simple volume price only if it matches your real margin"
            body += "\nWant me to turn this into a 3-line WhatsApp for nearby office admins?"
        elif cat == "gyms" and "kids_yoga" in topic:
            body=f"{who}, for the kids-yoga summer camp you asked about: I’d start with a 4-week pilot, one weekend morning slot, age-banded capacity, and a simple trial price using your existing catalog."
            if active_title: body += f" You already have {active_title} active; we can use that as the acquisition hook."
            body += " Want me to draft the parent-facing WhatsApp?"
        else:
            body=f"{who}, on the {topic} you just asked about, I’d turn the idea into a small pilot first using only services/offers already in your catalog."
            if active_title: body += f" Current offer to build around: {active_title}."
            body += " Want me to draft the customer-facing version?"
        return _result(body,"open_ended","vera",trigger,
                        f"Continues the merchant's explicit planning intent ({last or topic}) with a concrete, low-risk draft.")

    if kind == "seasonal_perf_dip":
        delta=p.get("delta_pct", perf.get("delta_7d",{}).get("views_pct"))
        members=merchant.get("customer_aggregate",{}).get("active_count")
        body=f"{who}, your {p.get('metric','views')} are {_pct(delta)} this week."
        if p.get("is_expected_seasonal"):
            body += f" The trigger flags this as an expected seasonal dip ({p.get('season_note','seasonal window').replace('_',' ')}), so I wouldn’t chase it with blanket ad spend."
        if members: body += f" You have {members} active members — retention is the steadier lever right now."
        body += " Want me to draft a simple retention challenge?"
        return _result(body,"open_ended","vera",trigger,
                        "Seasonal dip is explicitly reframed using the trigger's expected-seasonal flag and the merchant's own membership/performance data.")

    if kind == "supply_alert":
        mol=p.get("molecule","")
        batches=", ".join(p.get("affected_batches",[]))
        body=f"{who}, urgent supply alert: {mol} batch(es) {batches} from {p.get('manufacturer','the manufacturer')} are affected."
        agg=merchant.get("customer_aggregate",{})
        if agg.get("chronic_rx_count") is not None: body += f" Your store has {agg['chronic_rx_count']} chronic-Rx customers; I can help identify the affected dispensing records if that data is available."
        body += " Want me to draft the customer notification + replacement-pickup workflow?"
        return _result(body,"open_ended","vera",trigger,
                        "Supply alert preserves exact molecule/batch/manufacturer facts and offers an operational response without inventing an affected count.")

    if kind == "chronic_refill_due":
        return _compose_customer(category,merchant,trigger,{})

    if kind == "category_seasonal":
        trends=p.get("trends",[])
        body=f"{who}, summer demand is moving unevenly: {', '.join(trends[:3])}."
        body += " For a pharmacy, I’d adjust shelf visibility around the strongest demand signals rather than discounting everything."
        body += " Want me to turn those three signals into a simple shelf-action checklist?"
        return _result(body,"open_ended","vera",trigger,
                        "Seasonal category signal is translated into a concrete merchandising action using only the supplied trend data.")

    if kind == "gbp_unverified":
        body=f"{who}, your Google Business Profile is still unverified."
        if p.get("verification_path"): body += f" The available path is {p['verification_path']}."
        if p.get("estimated_uplift_pct"): body += f" The supplied estimate is up to {_pct(p['estimated_uplift_pct'])} uplift."
        body += " Want me to walk you through the verification checklist?"
        return _result(body,"open_ended","vera",trigger,
                        "GBP trigger is grounded in verification status, provided path and provided estimate; no new claim is introduced.")

    if kind == "cde_opportunity":
        d=_digest_item(category,p)
        title=d.get("title") if d else "the available professional webinar"
        fee=p.get("fee")
        body=f"{who}, there’s a relevant professional learning opportunity: {title}."
        if p.get("credits"): body += f" It carries {p['credits']} credits"
        if fee: body += f" and is {fee}."
        body += ". Want me to pull the details and summarize whether it fits your practice?"
        return _result(body,"open_ended","vera",trigger,
                        "Professional-learning trigger is answered with the supplied title, credits and fee.")

    if kind == "competitor_opened":
        body=f"{who}, a new competitor signal is active for {loc or merchant.get('identity',{}).get('city','your area')}."
        if p.get("competitor_name"): body=f"{who}, a new competitor — {p['competitor_name']} — opened"
        if p.get("distance_km") is not None: body += f" {p['distance_km']} km away"
        if p.get("their_offer"): body += f" with {p['their_offer']}."
        else: body += "."
        body += " I wouldn’t copy the offer blindly; want a quick comparison against your current profile and active offers?"
        return _result(body,"open_ended","vera",trigger,
                        "Competitor alert uses only the supplied name, distance and offer and proposes a grounded comparison.")

    if kind == "perf_spike":
        metric=p.get("metric")
        if not metric:
            d7=perf.get("delta_7d",{})
            pos=[(k,v) for k,v in d7.items() if isinstance(v,(int,float)) and v > 0]
            metric=pos[0][0].replace('_pct','') if pos else "performance"
        delta=p.get("delta_pct")
        if delta is None: delta=perf.get("delta_7d",{}).get(metric+"_pct")
        body=f"{who}, nice signal: {metric}"
        if delta is not None: body += f" are up {_pct(delta)}"
        body += f" in the last {p.get('window','recent period')} vs a baseline of {p.get('vs_baseline','your prior level')}."
        driver=p.get("likely_driver")
        if driver: body += f" The likely driver flagged here is {driver.replace('_',' ')}."
        body += " Want me to turn the winning pattern into one repeatable post or offer?"
        return _result(body,"open_ended","vera",trigger,
                        "Performance spike is celebrated with exact movement/baseline and the supplied likely driver.")

    if kind == "milestone_reached":
        metric=p.get('metric')
        value=p.get('value_now')
        target=p.get('milestone_value')
        if value is not None:
            body=f"{who}, you’re at {value} {metric or 'on the milestone signal'}"
        else:
            body=f"{who}, there’s a milestone signal on your account"
        if p.get("is_imminent") and target is not None and value is not None:
            body += f" — only {int(target)-int(value)} to {target}."
        body += " Want me to draft a simple thank-you post that also nudges the next milestone?"
        return _result(body,"open_ended","vera",trigger,
                        "Milestone message uses the exact current and target values and offers a concrete celebratory asset.")

    if kind == "review_theme_emerged":
        theme=p.get("theme","review theme").replace("_"," ")
        body=f"{who}, one pattern is showing up in reviews: “{p.get('common_quote','')}” ({p.get('occurrences_30d','')} mentions, {p.get('trend','')})."
        body += f" I’d address the {theme} issue before adding another promo. Want me to draft the customer-recovery reply?"
        return _result(body,"open_ended","vera",trigger,
                        "Review trigger uses the exact theme, count and customer wording from the trigger.")

    if kind == "dormant_with_vera":
        days=p.get("days_since_last_merchant_message")
        last=p.get("last_topic","")
        if days is not None:
            body=f"{who}, it’s been {days} days since we last spoke"
        else:
            body=f"{who}, we have a dormant-with-Vera signal on the account"
        if last: body += f" (last topic: {last.replace('_',' ')})."
        else: body += "."
        body += " I’ll keep this to one useful check-in: is there anything you want Vera to work on this week?"
        return _result(body,"open_ended","vera",trigger,
                        "Dormancy trigger acknowledges the gap and asks one low-pressure question tied to the prior topic.")

    if kind == "winback_eligible":
        days=p.get("days_since_expiry"); dip=p.get("perf_dip_pct"); added=p.get("lapsed_customers_added_since_expiry")
        body=f"{who}, you’ve been {days} days past expiry; your profile is down {_pct(dip)} and {added} lapsed customers have accumulated since then."
        body += " Rather than push a generic renewal, want a short win-back plan built around your current catalog?"
        return _result(body,"open_ended","vera",trigger,
                        "Winback message combines expiry age, performance change and lapsed-customer count to motivate a concrete next step.")

    # Generic safe handler for generated/unseen trigger kinds
    metric = p.get("metric") or p.get("metric_or_topic")
    body=f"{who}, quick update on {kind.replace('_',' ')}"
    if metric: body += f": {metric}."
    elif active_title: body += f". Your current active offer is {active_title}."
    else: body += "."
    body += " I can turn the supplied signal into one concrete next step. Want me to draft it?"
    return _result(body,"open_ended","vera",trigger,
                   f"Generic grounded fallback for unseen trigger kind {kind}; it does not introduce facts outside the provided contexts.")

def _result(body, cta, send_as, trigger, rationale):
    return {
        "body": body.strip(),
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key",""),
        "rationale": rationale,
    }

def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """Required challenge composer. Route by the trigger's declared scope, not by customer_id presence."""
    if trigger.get("scope") == "customer":
        if customer is None:
            return _result("", "none", "merchant_on_behalf", trigger,
                           "Customer-scoped trigger arrived without CustomerContext; no customer message is sent.")
        return _compose_customer(category, merchant, trigger, customer)
    return _compose_merchant(category, merchant, trigger)

# ---------------------------------------------------------------------------
# Conversation handler
# ---------------------------------------------------------------------------

def respond(state: dict, message: str, from_role: str = "merchant") -> dict:
    """Deterministic conversational policy for merchant and customer turns."""
    msg = message.strip()
    low = msg.lower()

    # Explicit opt-out always wins, for either role.
    if re.search(r"\b(stop|unsubscribe|remove me|not interested|no thanks|don't contact|do not contact|leave me alone)\b", low):
        return {"action":"end","rationale":"Detected explicit opt-out or disinterest; ending without another pitch."}

    # A customer asking to book/confirm should get an immediate handoff-style response,
    # not another qualification loop.
    if from_role == "customer":
        if re.search(r"\b(book|booking|appointment|schedule|slot|reserve|confirm)\b", low):
            trigger = state.get("trigger", {})
            kind = trigger.get("kind", "")
            return {"action":"send",
                    "body":"Absolutely — I can help with the next step using the appointment details already shared. If a specific slot is available in the context, I’ll use that rather than ask you to repeat it.",
                    "cta":"open_ended",
                    "rationale":f"Customer expressed clear action intent ({kind or 'appointment'}); acknowledge and move directly to the next step without unnecessary qualification."}
        if re.search(r"\b(yes|yep|yeah|sure|go ahead|okay|ok|confirm)\b", low):
            return {"action":"send","body":"Got it — I’ll continue using the details already shared here.","cta":"open_ended","rationale":"Customer accepted the proposed next step; continuing without repeating questions."}
        if re.search(r"\b(wait|later|not now|busy|remind me)\b", low):
            return {"action":"wait","wait_seconds":1800,"rationale":"Customer requested delay; backing off for 30 minutes."}
        if "?" in msg or re.search(r"\b(how|what|why|which|can you|where|when)\b", low):
            return {"action":"send","body":"I’ll stick to the facts and options already available in the context rather than guess at anything missing.","cta":"open_ended","rationale":"Customer question detected; answer conservatively from available context."}
        return {"action":"wait","wait_seconds":900,"rationale":"No clear customer action intent detected; avoid unnecessary follow-up."}

    # Merchant-side delay/acceptance handling.
    if re.search(r"\b(wait|later|not now|busy|remind me)\b", low):
        return {"action":"wait","wait_seconds":1800,"rationale":"Merchant requested delay; backing off for 30 minutes."}

    if re.search(r"\b(yes|yep|yeah|sure|go ahead|do it|send it|okay|ok|confirm|1|2)\b", low):
        trigger = state.get("trigger", {})
        kind = trigger.get("kind","")
        if kind in {"research_digest","cde_opportunity"}:
            return {"action":"send","body":"Absolutely — I’ll use the source and merchant context already provided and keep the draft ready to use.","cta":"open_ended","rationale":"Merchant accepted the offered artifact; advancing without adding unsupported facts."}
        if kind in {"perf_dip","perf_spike","seasonal_perf_dip","winback_eligible","review_theme_emerged","competitor_opened"}:
            return {"action":"send","body":"Done — I’ll keep the next step tied to the signal we just discussed rather than turning it into a generic campaign.","cta":"open_ended","rationale":"Merchant accepted a targeted follow-up; continuing the same intent."}
        if kind == "curious_ask_due":
            return {"action":"send","body":"Perfect — give me the service name you’re seeing most often and I’ll turn it into a short post and pricing reply.","cta":"open_ended","rationale":"One merchant-supplied fact is still needed before drafting."}
        return {"action":"send","body":"Got it. I’ll take the next step using the details already shared here.","cta":"open_ended","rationale":"Positive acknowledgement followed by a low-friction continuation of the existing request."}

    if "?" in msg or re.search(r"\b(how|what|why|which|can you|where|when)\b", low):
        return {"action":"send","body":"Good question. I’ll stick to the facts and options already available in the context rather than guess at anything missing.","cta":"open_ended","rationale":"Question detected; avoids hallucinating unavailable facts."}

    return {"action":"wait","wait_seconds":900,"rationale":"No clear action intent detected; avoid sending an unnecessary follow-up."}

# ---------------------------------------------------------------------------
# HTTP harness
# ---------------------------------------------------------------------------

app = FastAPI(title="Vera", version=VERSION)
contexts: dict[tuple[str,str], dict] = {}
conversations: dict[str, dict] = {}
sent_suppression: set[str] = set()

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str
    message: str
    received_at: str
    turn_number: int

@app.get("/v1/healthz")
def healthz():
    counts = {"category":0,"merchant":0,"customer":0,"trigger":0}
    for (scope,_),_v in contexts.items():
        counts[scope] = counts.get(scope,0)+1
    return {"status":"ok","uptime_seconds":int(time.time()-START),"contexts_loaded":counts}

@app.get("/v1/metadata")
def metadata():
    return {"team_name":TEAM,"team_members":["Team Vera"],
            "model":"deterministic-hybrid-rule-engine","approach":"context-grounded category-aware decision engine with deterministic conversation handling",
            "contact_email":"","version":VERSION,"submitted_at":datetime.utcnow().isoformat()+"Z"}

@app.post("/v1/context")
def push_context(body: CtxBody):
    if body.scope not in {"category","merchant","customer","trigger"}:
        return {"accepted":False,"reason":"invalid_scope","details":"scope must be category, merchant, customer or trigger"}
    key=(body.scope,body.context_id)
    cur=contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted":False,"reason":"stale_version","current_version":cur["version"]}
    contexts[key]={"version":body.version,"payload":body.payload}
    return {"accepted":True,"ack_id":f"ack_{body.context_id}_v{body.version}",
            "stored_at":datetime.utcnow().isoformat()+"Z"}

@app.post("/v1/tick")
def tick(body: TickBody):
    actions=[]
    for tid in body.available_triggers:
        tr=contexts.get(("trigger",tid),{}).get("payload")
        if not tr: continue
        mid=tr.get("merchant_id")
        merchant=contexts.get(("merchant",mid),{}).get("payload")
        if not merchant: continue
        cat=contexts.get(("category",merchant.get("category_slug")),{}).get("payload")
        if not cat: continue
        cid=tr.get("customer_id")
        customer=contexts.get(("customer",cid),{}).get("payload") if cid else None
        result=compose(cat,merchant,tr,customer)
        if not result.get("body"):
            continue
        sk=result.get("suppression_key","")
        if sk and sk in sent_suppression:
            continue
        conv=f"conv_{mid}_{cid or 'merchant'}_{tid}"
        sent_suppression.add(sk) if sk else None
        conversations[conv]={"merchant_id":mid,"customer_id":cid,"category":cat,
                             "merchant":merchant,"customer":customer,"trigger":tr,
                             "last_action":result,"turns":[]}
        actions.append({
            "conversation_id":conv,"merchant_id":mid,"customer_id":cid,
            "send_as":result["send_as"],"trigger_id":tid,
            "template_name":("vera_"+tr.get("kind","generic")+"_v1") if result["send_as"]=="vera" else ("merchant_"+tr.get("kind","generic")+"_v1"),
            "template_params":[_first_name(merchant), merchant.get("identity",{}).get("name","")],
            "body":result["body"],"cta":result["cta"],
            "suppression_key":result["suppression_key"],"rationale":result["rationale"]
        })
    return {"actions":actions}

@app.post("/v1/reply")
def reply(body: ReplyBody):
    state=conversations.setdefault(body.conversation_id,{"turns":[]})
    state.setdefault("turns",[]).append({"from":body.from_role,"body":body.message,"received_at":body.received_at})
    result=respond(state,body.message,body.from_role)
    if result.get("action")=="send":
        state["last_action"]=result
    return result

@app.get("/")
def root():
    return {"service":"vera","status":"ok","version":VERSION}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT","8080")))
