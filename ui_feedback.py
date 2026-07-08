"""Streamlit rendering for the Spotter-feedback merge/replace preview (Step-2 import gate).

Kept in its own importable module so the panel can be rendered both by app.py and by a
standalone eyeballing harness without executing the whole app.
"""
import streamlit as st


def render_feedback_panel(previews) -> bool:
    """Render the per-model feedback diff + the Merge/Replace control.

    previews: [{model, target_present, add[], replace[], keep[]}] from feedback_replace.feedback_preview.
    Returns replace_ack — whether the promotion may proceed (True unless Replace is chosen and not
    yet acknowledged). The chosen mode lives in st.session_state['feedback_mode'] (read by the
    import step); the Replace acknowledgment in st.session_state['ack_replace'].
    """
    st.markdown("#### Spotter feedback")
    def _show_grouped(title, grouped, present=True):
        st.markdown(f"**{title}**")
        if not present:
            st.caption("model not present on the target yet — it will be created")
            return
        shown = False
        for label, items in grouped.items():
            if items:
                shown = True
                st.markdown(f"_{label}_ ({len(items)}):")
                for it in items:
                    # Per-entry "?" tooltip shows the columns it maps to (search_tokens), like Step 1.
                    st.markdown(f"- {it['phrase']}",
                                help=(f"maps to columns: {it['tokens']}" if it.get("tokens") else None))
        if not shown:
            st.caption("none")

    for pv in previews:
        st.markdown(f"**{pv['model']}**")
        parts = []
        if pv["add"]:     parts.append(f"**{len(pv['add'])} new** added")
        if pv["replace"]: parts.append(f"**{len(pv['replace'])} updated** (same phrase)")
        if pv["keep"]:    parts.append(f"**{len(pv['keep'])} already on the target** the source doesn't have")
        st.caption("On promote: " + ("; ".join(parts) if parts
                                      else ("source has no feedback" if not pv["source"] else "no change")) + ".")
        if pv["keep"]:
            st.caption("Target-only (kept on Merge, dropped on Replace): "
                       + ", ".join(f"`{k}`" for k in pv["keep"]))
        # Dropdown to investigate the actual reference questions + business terms (source vs target),
        # grouped like the Step-1 picker.
        with st.expander(f"Investigate feedback — {pv['model']}  "
                         f"(source {len(pv['source'])} · target {len(pv['target'])})"):
            _show_grouped("Source (being promoted)", pv["source_grouped"], True)
            st.divider()
            _show_grouped("On the target now", pv["target_grouped"], pv["target_present"])

    any_target_only = any(pv["keep"] for pv in previews)
    mode = st.radio(
        "Feedback handling",
        ["Merge — keep the target's own feedback (default, safe)",
         "Replace — target ends with ONLY the source's feedback"],
        key="feedback_mode")

    replace_ack = True
    if mode.startswith("Replace"):
        st.warning(
            "**Replace rebuilds each model**: it moves the obj_id onto a fresh copy "
            "(clean feedback), re-points that model's dependents (answers/liveboards) onto it, "
            "then deletes the old model — only if it ends with no non-feedback dependents "
            "(otherwise it is kept and flagged). Target-only feedback is dropped."
            + ("" if any_target_only else
               "  Note: there is no target-only feedback here, so Replace and Merge give the "
               "same result."))
        replace_ack = st.checkbox(
            "I understand Replace rebuilds the model(s) and drops target-only feedback.",
            key="ack_replace")
    return replace_ack


def render_nl_panel(previews) -> bool:
    """Render the NL-instructions (Spotter coaching) merge/replace preview. Returns nl_ack.
    Mode in st.session_state['nl_mode']; Replace acknowledgment in 'ack_nl_replace'."""
    st.markdown("#### Spotter instructions (model coaching)")
    for pv in previews:
        st.markdown(f"**{pv['model']}**")
        st.caption(f"Source instructions ({len(pv['source'])}): "
                   + ("; ".join(f"“{s}”" for s in pv["source"]) if pv["source"] else "none"))
        if pv["target_present"]:
            st.caption(f"On the target now ({len(pv['target'])}): "
                       + ("; ".join(f"“{s}”" for s in pv["target"]) if pv["target"] else "none"))
        else:
            st.caption("On the target now: model not present yet — it will be created.")
        parts = []
        if pv["add"]:         parts.append(f"**{len(pv['add'])} new** added")
        if pv["target_only"]: parts.append(f"**{len(pv['target_only'])} already on the target** the source doesn't have")
        st.caption("On promote: " + ("; ".join(parts) if parts else "no change") + ".")

    any_target_only = any(pv["target_only"] for pv in previews)
    mode = st.radio(
        "Instruction handling",
        ["Merge — keep the target's own instructions (default, safe)",
         "Replace — target ends with ONLY the source's instructions"],
        key="nl_mode")
    st.caption("Only GLOBAL (model-level) instructions are promoted; any user-scoped instructions "
               "on the target are left untouched. Merge appends the source's instructions after "
               "the target's, so please review the instruction order on the target afterward — "
               "Spotter may weight earlier instructions more.")
    nl_ack = True
    if mode.startswith("Replace"):
        st.warning(
            "**Replace** sets the target's instructions to exactly the source's — the target's own "
            "instructions are dropped."
            + ("" if any_target_only else
               "  Note: there are no target-only instructions here, so Replace and Merge match."))
        nl_ack = st.checkbox("I understand Replace drops the target's own instructions.",
                             key="ack_nl_replace")
    return nl_ack
