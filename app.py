#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import base64, functools, os, mimetypes, re
from typing import List, Optional, Tuple
from flask import Flask, request, render_template, send_file, abort, url_for, jsonify, redirect, Response
import torch
from transformers import AutoTokenizer, AutoModel
from neo4j import GraphDatabase

# ---------------------------
# Config
# ---------------------------
BASIC_AUTH_USER = os.getenv("BASIC_AUTH_USER")
BASIC_AUTH_PASS = os.getenv("BASIC_AUTH_PASS")

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "livelongandprosper")
NEO4J_DB = os.getenv("NEO4J_DB", "neo4j")

EMBED_MODEL_NAME = os.getenv("EMBED_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2")
EMBED_BATCH = int(os.getenv("EMBED_BATCH", "32"))

# IMPORTANT: only serve media from within this base dir
MEDIA_BASE = os.getenv("MEDIA_BASE", "/mnt/8TB_2025/fileserver/audio")

# ANN index names (must exist)
IDX_TRANS_V1 = os.getenv("IDX_TRANS_V1", "transcription_embedding_index")
IDX_TRANS_V2 = os.getenv("IDX_TRANS_V2", "transcription_embedding_v2_index")
IDX_SEGMENT  = os.getenv("IDX_SEGMENT", "segment_embedding_index")
IDX_UTTER    = os.getenv("IDX_UTTER", "utterance_embedding_index")
IDX_GLOBAL   = os.getenv("IDX_GLOBAL", "global_speaker_embedding_index")  # GlobalSpeaker vector index

# Suggestion defaults
SUGG_TOPK   = int(os.getenv("SUGG_TOPK", "5"))
SUGG_THRESH = float(os.getenv("SUGG_THRESH", "0.62"))
SUGG_LIMIT_SPEAKERS = int(os.getenv("SUGG_LIMIT_SPEAKERS", "200"))

# ---------------------------
# Model (lazy — loaded on first embed)
# ---------------------------
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
_tokenizer = None
_model = None

def _get_embed_model():
    global _tokenizer, _model
    if _model is None:
        _tokenizer = AutoTokenizer.from_pretrained(EMBED_MODEL_NAME)
        _model = AutoModel.from_pretrained(EMBED_MODEL_NAME).to(DEVICE).eval()
    return _tokenizer, _model

def _normalize(v: torch.Tensor) -> torch.Tensor:
    return torch.nn.functional.normalize(v, p=2, dim=1)

def _mean_pooling(last_hidden_state: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    mask = attention_mask.unsqueeze(-1).expand(last_hidden_state.size()).float()
    return (last_hidden_state * mask).sum(dim=1) / torch.clamp(mask.sum(dim=1), min=1e-9)

def embed_texts(texts: List[str], batch_size: int = EMBED_BATCH, max_length: int = 512) -> List[List[float]]:
    if not texts: return []
    tok, mod = _get_embed_model()
    vecs = []
    with torch.no_grad():
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i+batch_size]
            enc = tok(batch, return_tensors="pt", padding=True, truncation=True, max_length=max_length)
            enc = {k: v.to(DEVICE) for k,v in enc.items()}
            out = mod(**enc)
            pooled = _mean_pooling(out.last_hidden_state, enc["attention_mask"])
            pooled = _normalize(pooled)
            vecs.extend(pooled.cpu().numpy().tolist())
    return vecs

# ---------------------------
# Neo4j
# ---------------------------
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

def query_transcriptions_ab_with_best_utterance(qvec, k=10, m=200, mode="both"):
    with driver.session(database=NEO4J_DB) as sess:
        CYPHER = """
        CALL {
            WITH $qvec AS q, $k AS k, $idx AS idx
            CALL db.index.vector.queryNodes(idx, k, q)
            YIELD node AS t, score AS t_score
            RETURN collect({t: t, t_score: t_score}) AS t_hits
        }

        CALL {
            WITH $qvec AS q, $m AS m, $utt_idx AS uidx
            CALL db.index.vector.queryNodes(uidx, m, q)
            YIELD node AS u, score AS u_score
            MATCH (t:Transcription)-[:HAS_UTTERANCE]->(u)
            OPTIONAL MATCH (u)-[:OF_SEGMENT]->(s:Segment)

            // 1) Direct SPOKEN_BY
            OPTIONAL MATCH (u)-[:SPOKEN_BY]->(sp:Speaker)

            WITH t,u,u_score,s,sp,
                 CASE WHEN sp IS NOT NULL THEN sp.id ELSE s.speaker_id END AS sp_id,
                 CASE WHEN sp IS NOT NULL THEN sp.label ELSE s.speaker_label END AS sp_label,
                 s.is_lyrics AS is_lyrics, s.lyrics_score AS lyrics_score, s.review_needed AS review_needed

            // 2) Hop to GlobalSpeaker
            OPTIONAL MATCH (spx:Speaker {id:sp_id})-[:SAME_PERSON]->(gs:GlobalSpeaker)
            WITH t,u,u_score,s,sp_id,sp_label,is_lyrics,lyrics_score,review_needed,
                 collect(DISTINCT {gs_id: gs.id, status: gs.status, method: gs.method, display_label: gs.display_label}) AS globals

            RETURN collect({
                t_id: t.id, t_key: t.key,
                t_media: t.source_media, t_rttm: t.source_rttm, t_started_at: t.started_at,
                u_id: u.id, u_text: u.text, u_start: u.start, u_end: u.end,
                u_abs_start: coalesce(u.absolute_start, s.absolute_start),
                u_abs_end:   coalesce(u.absolute_end, s.absolute_end),
                s_id: s.id, s_idx: s.idx, s_start: s.start, s_end: s.end,
                is_lyrics: is_lyrics, lyrics_score: lyrics_score, review_needed: review_needed,
                speakers: CASE WHEN sp_id IS NOT NULL
                           THEN [{id: sp_id, label: sp_label, globals: globals}]
                           ELSE []
                           END,
                u_score: u_score
            }) AS utt_hits
        }

        WITH t_hits, utt_hits
        UNWIND t_hits AS th
        WITH th, [uh IN utt_hits WHERE uh.t_id = th.t.id] AS utt_for_t
        UNWIND CASE WHEN size(utt_for_t)=0 THEN [NULL] ELSE utt_for_t END AS uh
        WITH th, uh
        ORDER BY uh.u_score DESC
        WITH th, collect(uh)[0] AS best
        RETURN
            th.t.id            AS t_id,
            th.t.key           AS t_key,
            best.t_started_at  AS t_started_at,
            best.t_media       AS t_media,
            best.t_rttm        AS t_rttm,
            th.t_score         AS t_score,
            best.u_id          AS u_id,
            best.u_text        AS u_text,
            best.u_start       AS u_start,
            best.u_end         AS u_end,
            best.u_abs_start   AS u_abs_start,
            best.u_abs_end     AS u_abs_end,
            best.speakers      AS speakers,
            best.s_id          AS s_id,
            best.s_idx         AS s_idx,
            best.s_start       AS s_start,
            best.s_end         AS s_end,
            best.is_lyrics     AS is_lyrics,
            best.lyrics_score  AS lyrics_score,
            best.review_needed AS review_needed,
            best.u_score       AS u_score
        ORDER BY t_score DESC
        LIMIT $k;
        """

        out = {}
        if mode in ("both", "v1"):
            out["v1"] = sess.run(CYPHER, qvec=qvec, k=k, m=m, idx=IDX_TRANS_V1, utt_idx=IDX_UTTER).data()
        else:
            out["v1"] = []

        if mode in ("both", "v2"):
            out["v2"] = sess.run(CYPHER, qvec=qvec, k=k, m=m, idx=IDX_TRANS_V2, utt_idx=IDX_UTTER).data()
        else:
            out["v2"] = []
        return out

# ---------------------------
# Media helpers
# ---------------------------
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv"}

def _is_safe_media(path: str) -> bool:
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(MEDIA_BASE)
        return real.startswith(base) and os.path.isfile(real)
    except Exception:
        return False

def _stem_and_dir(path: str) -> Tuple[str, str]:
    if not path: return "", ""
    d, fn = os.path.split(path)
    stem, _ = os.path.splitext(fn)
    stem = re.sub(r"_(F|R)$", "", stem)
    return stem, d

def best_audio_for_media(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    if not _is_safe_media(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext in AUDIO_EXTS:
        return path
    if ext in VIDEO_EXTS:
        stem, d = _stem_and_dir(path)
        if stem and d:
            for cand_ext in (".mp3", ".wav", ".m4a"):
                cand = os.path.join(d, stem + cand_ext)
                if _is_safe_media(cand):
                    return cand
        return path
    return path if _is_safe_media(path) else None

# ---------------------------
# Flask
# ---------------------------
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

if BASIC_AUTH_USER and BASIC_AUTH_PASS:
    @app.before_request
    def _require_auth():
        auth = request.authorization
        if not auth or auth.username != BASIC_AUTH_USER or auth.password != BASIC_AUTH_PASS:
            return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="auto-assign"'})

@app.context_processor
def _inject_helpers():
    # Expose helper into Jinja
    return dict(best_audio_for_media=best_audio_for_media)

# --------- Home / Search ----------
@app.route("/", methods=["GET"])
def home():
    return render_template("index.html", results=None, q=None, k=10, m=200, mode="both")

@app.route("/search", methods=["POST"])
def search():
    q = (request.form.get("q") or "").strip()
    k = int(request.form.get("k") or 10)
    m = int(request.form.get("m") or 200)
    mode = (request.form.get("mode") or "both").lower()
    if not q:
        return render_template("index.html", results=None, q=q, k=k, m=m, mode=mode)
    qvec = embed_texts([q])[0]
    results = query_transcriptions_ab_with_best_utterance(qvec, k=k, m=m, mode=mode)
    return render_template("index.html", results=results, q=q, k=k, m=m, mode=mode)

# ---------- MEDIA ----------
@app.get("/media")
def serve_media():
    path = request.args.get("path", "")
    if not _is_safe_media(path):
        abort(404)
    guessed = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return send_file(path, mimetype=guessed, as_attachment=False, conditional=True)

# ---------- LABELING & CLUSTERS ----------
@app.post("/speaker/rename")
def speaker_rename():
    sp_id = (request.form.get("speaker_id") or "").strip()
    new_label = (request.form.get("new_label") or "").strip()
    if not sp_id or not new_label:
        return jsonify({"ok": False, "error": "speaker_id and new_label required"}), 400
    cy = """
    MATCH (sp:Speaker {id:$id})
    SET sp.label = $label, sp.updated_at = datetime()
    WITH sp
    MATCH (s:Segment) WHERE s.speaker_id = $id
    SET s.speaker_label = $label
    RETURN sp.id AS id, sp.label AS label
    """
    with driver.session(database=NEO4J_DB) as sess:
        rec = sess.run(cy, id=sp_id, label=new_label).single()
        if not rec:
            return jsonify({"ok": False, "error": "Speaker not found"}), 404
        return jsonify({"ok": True, "id": rec["id"], "label": rec["label"]})

@app.post("/global_speaker/rename")
def global_speaker_rename():
    gs_id = (request.form.get("global_id") or "").strip()
    new_label = (request.form.get("display_label") or "").strip()
    if not gs_id or not new_label:
        return jsonify({"ok": False, "error": "global_id and display_label required"}), 400
    cy = """
    MATCH (g:GlobalSpeaker {id:$id})
    SET g.display_label = $label, g.updated_at = datetime()
    WITH g
    OPTIONAL MATCH (sp:Speaker)-[:SAME_PERSON]->(g)
    SET sp.label = coalesce(sp.label, $label)
    WITH g, collect(sp.id) AS sp_ids
    MATCH (s:Segment) WHERE s.speaker_id IN sp_ids
    SET s.speaker_label = coalesce(s.speaker_label, $label)
    RETURN g.id AS id, g.display_label AS label, sp_ids
    """
    with driver.session(database=NEO4J_DB) as sess:
        rec = sess.run(cy, id=gs_id, label=new_label).single()
        if not rec:
            return jsonify({"ok": False, "error": "GlobalSpeaker not found"}), 404
        return jsonify({"ok": True, "id": rec["id"], "label": rec["label"], "affected_speakers": rec["sp_ids"]})

@app.post("/speaker/attach_global")
def speaker_attach_global():
    sp_id = (request.form.get("speaker_id") or "").strip()
    gs_id = (request.form.get("global_id") or "").strip()
    if not sp_id or not gs_id:
        return jsonify({"ok": False, "error": "speaker_id and global_id required"}), 400
    cy = """
    MATCH (sp:Speaker {id:$sp_id})
    MATCH (g:GlobalSpeaker {id:$gs_id})
    MERGE (sp)-[:SAME_PERSON]->(g)
    SET sp.updated_at = datetime(), g.updated_at = datetime()
    RETURN sp.id AS sp_id, g.id AS gs_id, g.display_label AS label
    """
    with driver.session(database=NEO4J_DB) as sess:
        rec = sess.run(cy, sp_id=sp_id, gs_id=gs_id).single()
        if not rec:
            return jsonify({"ok": False, "error": "Failed to attach"}), 400
        return jsonify({"ok": True, "speaker_id": rec["sp_id"], "global_id": rec["gs_id"], "global_label": rec["label"]})

@app.post("/speaker/promote_to_global")
def speaker_promote_to_global():
    sp_id = (request.form.get("speaker_id") or "").strip()
    display_label = (request.form.get("display_label") or "").strip()
    if not sp_id:
        return jsonify({"ok": False, "error": "speaker_id required"}), 400
    cy = """
    MERGE (sp:Speaker {id:$sp_id})
    ON MATCH SET sp.updated_at = datetime()
    WITH sp
    MERGE (g:GlobalSpeaker {id:sp.id})
    ON CREATE SET g.created_at = datetime(), g.method='manual', g.status='tentative'
    SET g.updated_at = datetime(),
        g.display_label = coalesce($display_label, g.display_label)
    MERGE (sp)-[:SAME_PERSON]->(g)
    RETURN sp.id AS sp_id, g.id AS gs_id, g.display_label AS label
    """
    with driver.session(database=NEO4J_DB) as sess:
        rec = sess.run(cy, sp_id=sp_id, display_label=display_label or None).single()
        return jsonify({"ok": True, "speaker_id": rec["sp_id"], "global_id": rec["gs_id"], "global_label": rec["label"]})

@app.post("/cluster/propagate_labels")
def cluster_propagate_labels():
    gs_id = (request.form.get("global_id") or "").strip()
    if not gs_id:
        return jsonify({"ok": False, "error": "global_id required"}), 400
    cy = """
    MATCH (g:GlobalSpeaker {id:$id})
    OPTIONAL MATCH (sp:Speaker)-[:SAME_PERSON]->(g)
    SET sp.label = coalesce(g.display_label, sp.label),
        sp.updated_at = datetime()
    WITH g, collect(sp.id) AS sp_ids, g.display_label AS lbl
    MATCH (s:Segment) WHERE s.speaker_id IN sp_ids
    SET s.speaker_label = coalesce(s.speaker_label, lbl)
    RETURN lbl AS label, sp_ids
    """
    with driver.session(database=NEO4J_DB) as sess:
        rec = sess.run(cy, id=gs_id).single()
        return jsonify({"ok": True, "label": rec["label"], "affected_speakers": rec["sp_ids"]})

# ---------- SUGGESTIONS ----------
def _vector_suggestions(seed: Optional[str], topk: int, thresh: float, limit: int):
    cy = """
    // Candidate Speakers (unclustered, or focus on seed)
    CALL {
      WITH $seed AS seed, $limit AS lim
      MATCH (sp:Speaker)
      WHERE sp.embedding IS NOT NULL
        AND (seed IS NOT NULL AND sp.id = seed
             OR seed IS NULL AND NOT (sp)-[:SAME_PERSON]->(:GlobalSpeaker))
      RETURN sp
      LIMIT CASE WHEN seed IS NULL THEN lim ELSE 1 END
    }
    WITH sp
    CALL db.index.vector.queryNodes($gs_idx, $topk, sp.embedding)
      YIELD node AS gs, score
    WITH sp, gs, score
    WHERE score >= $thresh
    RETURN sp.id AS sp_id,
           sp.label AS sp_label,
           collect({
             gs_id: gs.id,
             display_label: gs.display_label,
             status: gs.status,
             method: gs.method,
             score: score
           }) AS cands
    ORDER BY size(cands) DESC, sp_id
    """
    with driver.session(database=NEO4J_DB) as sess:
        rows = sess.run(cy, seed=seed, gs_idx=IDX_GLOBAL, topk=topk, thresh=thresh, limit=limit).data()
        for r in rows:
            r["cands"] = sorted(r["cands"], key=lambda x: x["score"], reverse=True)
        return rows

@app.get("/suggest")
def suggest_page():
    topk = int(request.args.get("topk") or SUGG_TOPK)
    thresh = float(request.args.get("thresh") or SUGG_THRESH)
    limit = int(request.args.get("limit") or SUGG_LIMIT_SPEAKERS)
    seed = (request.args.get("seed") or "").strip() or None
    suggestions = _vector_suggestions(seed, topk, thresh, limit)
    return render_template("suggest.html",
                           suggestions=suggestions, topk=topk, thresh=thresh, limit=limit, seed=seed)

@app.post("/suggest/attach")
def suggest_attach_one():
    sp_id = (request.form.get("speaker_id") or "").strip()
    gs_id = (request.form.get("global_id") or "").strip()
    if not sp_id or not gs_id:
        return jsonify({"ok": False, "error": "speaker_id and global_id required"}), 400
    cy = """
    MATCH (sp:Speaker {id:$sp_id})
    MATCH (g:GlobalSpeaker {id:$gs_id})
    MERGE (sp)-[:SAME_PERSON]->(g)
    SET sp.updated_at = datetime(), g.updated_at = datetime()
    RETURN sp.id AS sp_id, g.id AS gs_id, g.display_label AS label
    """
    with driver.session(database=NEO4J_DB) as sess:
        rec = sess.run(cy, sp_id=sp_id, gs_id=gs_id).single()
        if not rec:
            return jsonify({"ok": False, "error": "Failed to attach"}), 400
        return redirect(url_for("suggest_page"))

@app.post("/suggest/attach_bulk")
def suggest_attach_bulk():
    pairs = request.form.getlist("pair")
    if not pairs:
        return jsonify({"ok": False, "error": "no selections"}), 400
    cy = """
    UNWIND $pairs AS pr
    WITH split(pr, "|") AS parts
    WITH parts[0] AS sp_id, parts[1] AS gs_id
    MATCH (sp:Speaker {id:sp_id})
    MATCH (g:GlobalSpeaker {id:gs_id})
    MERGE (sp)-[:SAME_PERSON]->(g)
    SET sp.updated_at = datetime(), g.updated_at = datetime()
    RETURN count(*) AS attached
    """
    with driver.session(database=NEO4J_DB) as sess:
        _ = sess.run(cy, pairs=pairs).single()
    return redirect(url_for("suggest_page"))

# ---------- FULL TRANSCRIPT PAGE ----------
@app.get("/t/<t_id>")
def transcription_full(t_id: str):
    cy = """
    MATCH (t:Transcription {id:$id})
    OPTIONAL MATCH (t)-[:HAS_UTTERANCE]->(u:Utterance)
    OPTIONAL MATCH (u)-[:OF_SEGMENT]->(s:Segment)
    OPTIONAL MATCH (u)-[:SPOKEN_BY]->(sp:Speaker)
    WITH t,u,s,sp,
         CASE WHEN sp IS NOT NULL THEN sp ELSE NULL END AS direct_sp,
         s.speaker_id AS seg_sid, s.speaker_label AS seg_slabel,
         s.is_lyrics AS is_lyrics, s.lyrics_score AS lyrics_score, s.review_needed AS review_needed
    WITH t,u,s,
         CASE WHEN direct_sp IS NOT NULL
              THEN {id: direct_sp.id, label: direct_sp.label}
              ELSE CASE WHEN seg_sid IS NOT NULL
                        THEN {id: seg_sid, label: seg_slabel}
                        ELSE NULL END
         END AS sp_map,
         is_lyrics, lyrics_score, review_needed
    OPTIONAL MATCH (sp2:Speaker {id:coalesce(sp_map.id, '__none__')})-[:SAME_PERSON]->(g:GlobalSpeaker)
    WITH t,u,s,sp_map,
         collect(DISTINCT {gs_id:g.id, display_label:g.display_label, method:g.method, status:g.status}) AS globals,
         is_lyrics, lyrics_score, review_needed
    ORDER BY u.start ASC
    RETURN t AS t,
           collect({
             id: u.id,
             idx: u.idx,
             start: u.start, end: u.end,
             text: u.text,
             speaker: sp_map,
             globals: [g IN globals WHERE g.gs_id IS NOT NULL],
             is_lyrics:is_lyrics, lyrics_score:lyrics_score, review_needed:review_needed,
             seg_idx: s.idx
           }) AS utterances
    """
    with driver.session(database=NEO4J_DB) as sess:
        rec = sess.run(cy, id=t_id).single()
        if not rec or not rec["t"]:
            abort(404)
        t = rec["t"]
        utts = rec["utterances"] or []
    return render_template("transcript.html", t=t, utterances=utts)

# ---------- MAIN ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=True)
