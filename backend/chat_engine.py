import os
import json
import time
import pandas as pd
import xgboost as xgb
from google import genai
from google.genai import types
import chromadb
from chromadb.utils import embedding_functions
from config import CHROMA_PERSIST_DIR, CHROMA_COLLECTION_NAME, EMBEDDING_MODEL
from pathlib import Path
from models import SessionLocal, ProblemEditorial
from typing import TypedDict
from langgraph.graph import StateGraph, START, END

cross_encoder_model = None

def get_cross_encoder():
    global cross_encoder_model
    if cross_encoder_model is None:
        from sentence_transformers import CrossEncoder
        print("Loading CrossEncoder model...")
        cross_encoder_model = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')
    return cross_encoder_model

# Suppress warnings
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

BASE_DIR = Path(__file__).resolve().parent

# --- Global Loaders ---
collection = None
def get_chroma_collection():
    global collection
    if collection is None:
        try:
            ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)
            chroma_client = chromadb.PersistentClient(path=str(BASE_DIR / "chroma_data"))
            collection = chroma_client.get_collection(name=CHROMA_COLLECTION_NAME, embedding_function=ef)
        except Exception as e:
            print(f"Warning: Could not load ChromaDB: {e}")
    return collection

ranker_model = xgb.XGBClassifier()
try:
    # Load the V2 model we just trained
    ranker_model.load_model(str(BASE_DIR.parent / "train" / "xgboost_ranker_v2.json"))
except Exception as e:
    print(f"Warning: Could not load XGBoost Ranker: {e}")

# --- Helper Functions ---
def clean_json_response(raw_text):
    text = raw_text.strip()
    # Using string multiplication to prevent UI markdown parser glitches
    json_prefix = "`" * 3 + "json"
    bt_prefix = "`" * 3
    
    if text.startswith(json_prefix):
        text = text[7:]
    if text.startswith(bt_prefix):
        text = text[3:]
    if text.endswith(bt_prefix):
        text = text[:-3]
    return text.strip()

def safe_gemini_call(client, model, contents, config=None, retries=3):
    """Wrapper to handle 503 and 429 rate limit errors."""
    for attempt in range(retries):
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except Exception as e:
            if "503" in str(e) or "429" in str(e):
                if attempt < retries - 1:
                    sleep_time = 2 ** attempt
                    time.sleep(sleep_time)
                    continue
            raise e

def get_target_rating(user_profile):
    avg_rating = int(user_profile.get("avg_rating", 1200))
    curr_rating = int(user_profile.get("current_rating", 0))
    return avg_rating if curr_rating == 0 else int((avg_rating + curr_rating) / 2)

def get_weakest_domain(metrics):
    if not metrics: return "arrays logic"
    domains = {
        "math": metrics.get("math_pref", 1),
        "dynamic programming": metrics.get("dp_pref", 1),
        "graphs and trees": metrics.get("graph_pref", 1),
        "binary search": metrics.get("binary_pref", 1),
        "data structures": metrics.get("datastruct_pref", 1)
    }
    return min(domains, key=domains.get)

# --- Main Engine ---
def fetch_practice_problem(user_profile: dict):
    """Straight-line execution: Build Query -> Fetch Candidates -> Rank -> Present."""
    
    api_key_1 = os.getenv("GEMINI_API_KEY")
    api_key_2 = os.getenv("GEMINI_API_KEY_2", api_key_1)  # Falls back to key 1 if not set
    client_query = genai.Client(api_key=api_key_1)
    client_present = genai.Client(api_key=api_key_2)
    
    system_prompt = user_profile.get("system_prompt", "You are an AI competitive programming coach.")
    question_rating = get_target_rating(user_profile)
    weakness_fallback = get_weakest_domain(user_profile.get("metrics", {}))

    # =====================================================================
    # STEP 1: QUERY BUILDER (LLM) - NO INTENT ROUTING NEEDED
    # =====================================================================
    query_builder_prompt = f"""
    {system_prompt}
    
    TASK: Generate a database search query to find the perfect practice problem for this user.
    Target their specific algorithmic weaknesses based on their ML Profile.
    
    You MUST respond with ONLY a valid JSON object. Do not add markdown or text.
    Format:
    {{"search_query": "specific algorithmic keywords", "min_rating": {max(800, question_rating - 100)}, "max_rating": {question_rating + 200}}}
    """
    
    try:
        router_res = safe_gemini_call(
            client=client_query,
            model='gemini-3.1-flash-lite-preview',
            contents=query_builder_prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        parsed_intent = json.loads(clean_json_response(router_res.text))
    except Exception as e:
        print(f"[*] Query Builder failed, using ML fallback. Error: {e}")
        parsed_intent = {
            "search_query": weakness_fallback,
            "min_rating": max(800, question_rating - 100),
            "max_rating": question_rating + 200
        }
    
    # =====================================================================
    # STEP 2: CANDIDATE GENERATION (ChromaDB)
    # =====================================================================
    search_query = parsed_intent.get("search_query", weakness_fallback)
    min_rating = int(parsed_intent.get("min_rating", max(800, question_rating - 100)))
    max_rating = int(parsed_intent.get("max_rating", question_rating + 200))
    
    col = get_chroma_collection()
    context_str = ""
    problem_details = None
    
    if col:
        try:
            # Over-fetch 150 problems to give XGBoost plenty of options and ensure we find one with an editorial
            results = col.query(
                query_texts=[search_query],
                n_results=150, 
                where={"$and": [{"rating": {"$gte": min_rating}}, {"rating": {"$lte": max_rating}}]}
            )
            
            raw_candidates = results.get("metadatas", [[]])[0]
            raw_docs = results.get("documents", [[]])[0]
            
            # =====================================================================
            # STEP 3: NEGATIVE PRUNING & FEATURE ASSEMBLY
            # =====================================================================
            attempted_set = set(user_profile.get("attempted_problems", []))
            metrics = user_profile.get("metrics", {})
            scoring_data = []
            fresh_candidates = []
            fresh_docs = []
            
            db = SessionLocal()
            try:
                editorial_records = db.query(ProblemEditorial.problem_id).all()
                editorial_set = set(r[0] for r in editorial_records)
            except:
                editorial_set = set()
            finally:
                db.close()
            
            fresh_candidates_with_editorial = []
            fresh_candidates_any = []
            
            for idx, meta in enumerate(raw_candidates):
                pid = meta.get("problem_id")
                
                # Only process problems the user has NEVER submitted code for
                if pid not in attempted_set:
                    fresh_candidates_any.append((meta, raw_docs[idx]))
                    
                    has_editorial = pid in editorial_set if len(editorial_set) > 0 else True
                    if has_editorial:
                        fresh_candidates_with_editorial.append((meta, raw_docs[idx]))
            
            # Prefer candidates with editorials, but fallback to any fresh candidate
            if fresh_candidates_with_editorial:
                fresh_candidates = [item[0] for item in fresh_candidates_with_editorial]
                fresh_docs = [item[1] for item in fresh_candidates_with_editorial]
            else:
                fresh_candidates = [item[0] for item in fresh_candidates_any]
                fresh_docs = [item[1] for item in fresh_candidates_any]
            
            for meta in fresh_candidates:
                tags = meta.get("tags", "").lower()
                rating = int(meta.get("rating", 0))
                
                # Compute Binary Tags
                is_dp = 1 if "dp" in tags else 0
                is_math = 1 if any(t in tags for t in ["math", "number theory", "combinatorics"]) else 0
                is_graph = 1 if any(t in tags for t in ["graphs", "trees", "dfs"]) else 0
                is_brute = 1 if any(t in tags for t in ["brute force", "implementation", "hashing"]) else 0
                is_greedy = 1 if any(t in tags for t in ["greedy", "two pointers", "sortings"]) else 0
                is_binary = 1 if "binary search" in tags else 0
                is_cons = 1 if any(t in tags for t in ["constructive", "strings", "interactive"]) else 0
                is_datastruct = 1 if any(t in tags for t in ["data structures", "dsu"]) else 0

                # Assemble the exact 20-feature row for XGBoost
                row = {
                    'accuracy': metrics.get('accuracy', 0.5),
                    'optimization_struggle': metrics.get('optimization_struggle', 0),
                    'avg_solved_rating': metrics.get('avg_solved_rating', 1200),
                    'abandonment_rate': metrics.get('abandonment_rate', 0),
                    'one_shot_rate': metrics.get('one_shot_rate', 0),
                    'tilt_speed_seconds': metrics.get('tilt_speed_seconds', 900),
                    'recent_win_rate': metrics.get('recent_win_rate', 0.5),
                    'persistence_index': metrics.get('persistence_index', 1.0),
                    'problem_rating': rating,
                    'global_solve_rate': meta.get('global_solve_rate', 0.45), # Safe default
                    'avg_attempts_per_user': meta.get('avg_attempts_per_user', 2.0), # Safe default
                    'rating_delta': rating - metrics.get('avg_solved_rating', 1200),
                    'dp_synergy': is_dp * metrics.get('dp_pref', 0),
                    'math_synergy': is_math * metrics.get('math_pref', 0),
                    'graph_synergy': is_graph * metrics.get('graph_pref', 0),
                    'brute_synergy': is_brute * metrics.get('brute_pref', 0),
                    'greedy_synergy': is_greedy * metrics.get('greedy_pref', 0),
                    'binary_synergy': is_binary * metrics.get('binary_pref', 0),
                    'cons_synergy': is_cons * metrics.get('cons_pref', 0),
                    'datastruct_synergy': is_datastruct * metrics.get('datastruct_pref', 0)
                }
                scoring_data.append(row)
            
            # =====================================================================
            # STEP 4: XGBOOST RANKING
            # =====================================================================
            if not fresh_candidates:
                context_str = "Error: User has solved all matching problems in this bracket."
            else:
                df_scoring = pd.DataFrame(scoring_data)
                
                # Enforce exact column order from training
                ordered_features = ['accuracy', 'optimization_struggle', 'avg_solved_rating', 'abandonment_rate', 'one_shot_rate', 'tilt_speed_seconds', 'recent_win_rate', 'persistence_index', 'problem_rating', 'global_solve_rate', 'avg_attempts_per_user', 'rating_delta', 'dp_synergy', 'math_synergy', 'graph_synergy', 'brute_synergy', 'greedy_synergy', 'binary_synergy', 'cons_synergy', 'datastruct_synergy']
                df_scoring = df_scoring[ordered_features]
                
                # Predict probability of success (y=1)
                probabilities = ranker_model.predict_proba(df_scoring)[:, 1]
                
                for i in range(len(fresh_candidates)):
                    fresh_candidates[i]["xgb_score"] = float(probabilities[i])
                    fresh_candidates[i]["doc_text"] = fresh_docs[i]
                    
                ranked_candidates = sorted(fresh_candidates, key=lambda x: x["xgb_score"], reverse=True)
                winning_problem = ranked_candidates[0]
                
                problem_details = winning_problem
                doc = winning_problem.pop("doc_text")
                score = winning_problem.pop("xgb_score")
                
                print(f"[+] XGBoost selected {problem_details.get('name')} (Confidence Score: {score:.3f})")
                context_str = f"**Problem Retrieved from Database:** {problem_details.get('name', 'Unknown')}\n**Rating:** {problem_details.get('rating', 'Unrated')}\n**Tags:** {problem_details.get('tags', '')}\n\n**Problem Description:**\n{doc}"

        except Exception as e:
            print(f"Chroma/XGBoost Pipeline Error: {e}")
            context_str = "Database connection or ranking error."

    # =====================================================================
    # STEP 5: THE COACH'S PRESENTATION (LLM)
    # =====================================================================
    if not problem_details:
        return {"message": "I couldn't find a fresh problem for your specific constraints. Try broadening your practice!", "problem_details": None}

    presentation_prompt = f"""
    {system_prompt}
    
    You are presenting this freshly retrieved Codeforces problem to the user:
    {context_str}
    
    INSTRUCTION: Introduce this problem perfectly in your persona's character. 
    Explain exactly WHY you chose this specific problem based on their ML profile weaknesses. 
    Do not give them the solution. Be concise and format nicely with markdown.
    """
    
    try:
        final_response = safe_gemini_call(
            client=client_present,
            model='gemini-3.1-flash-lite-preview',
            contents=presentation_prompt
        )
        return {"message": final_response.text, "problem_details": problem_details}
    except Exception as e:
        print(f"Presentation LLM Error: {e}")
        fallback_msg = f"**I found a problem for you: {problem_details.get('name')}**\n\n*(Your AI Coach is experiencing server load, but here is the raw problem)*\n\n{context_str}"
        return {"message": fallback_msg, "problem_details": problem_details}

def get_socratic_hint(problem_id: str, current_prompt: str = ""):
    """Fetches the official editorial and prompts Gemini to give a small Socratic hint."""
    db = SessionLocal()
    try:
        record = db.query(ProblemEditorial).filter(ProblemEditorial.problem_id == problem_id).first()
        editorial = record.editorial_text if record else None
    finally:
        db.close()

    if not editorial:
        return {"error": "No official editorial available for this problem yet. Keep trying!"}

    hint_prompt = f"""
    You are a world-class Socratic competitive programming coach.
    The user is currently stuck on a problem and has asked for a hint.

    Here is the official editorial / solution for this problem:
    {editorial}

    INSTRUCTION: Provide the user with a single logical hint. DO NOT reveal the full algorithm or give away code. Guide them to the next step of the thought process. Be encouraging.
    """

    api_key_1 = os.getenv("GEMINI_API_KEY")
    client_present = genai.Client(api_key=api_key_1)

    try:
        res = safe_gemini_call(client=client_present, model='gemini-3.1-flash-lite-preview', contents=hint_prompt)
        return {"message": res.text}
    except Exception as e:
        return {"error": f"LLM error: {e}"}



class DebugState(TypedDict):
    user_code: str
    editorial: str
    bug_report: str
    draft_response: str
    final_response: str
    loop_count: int
    is_safe: bool

def critic_node(state: DebugState):
    """Analyzes the code and finds the exact bug logically."""
    prompt = f"""
    You are an expert Algorithmic Code Critic.
    User Code:
    ```
    {state['user_code']}
    ```
    Official Editorial:
    {state['editorial']}
    
    INSTRUCTION: Find the exact logical or complexity flaw in the user's code compared to the editorial.
    Output a highly technical bug report. DO NOT talk to the user.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    res = safe_gemini_call(client=client, model='gemini-3.1-flash-lite-preview', contents=prompt)
    return {"bug_report": res.text}

def coach_node(state: DebugState):
    """Turns the technical bug report into a pedagogical hint."""
    prompt = f"""
    You are a Socratic Competitive Programming Coach.
    Technical Bug Report: {state['bug_report']}
    
    INSTRUCTION: The user has a bug. Write a gentle, pedagogical response that helps them realize the flaw themselves.
    Ask guiding questions.
    DO NOT write any solution code for them!
    """
    if state.get("loop_count", 0) > 0:
        prompt += "\nWARNING: Your previous response contained actual solution code. You MUST remove all solution code and only give hints."
        
    api_key = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    res = safe_gemini_call(client=client, model='gemini-3.1-flash-lite-preview', contents=prompt)
    return {"draft_response": res.text, "loop_count": state.get("loop_count", 0) + 1}

def reviewer_node(state: DebugState):
    """Checks if the draft response leaks solution code."""
    prompt = f"""
    You are a Safety Reviewer.
    Coach's Draft Response:
    {state['draft_response']}
    
    Did the coach include actual C++, Python, or Java solution code that gives away the answer?
    Reply strictly with "SAFE" if it's just hints, or "UNSAFE" if it contains solution code blocks.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    res = safe_gemini_call(client=client, model='gemini-3.1-flash-lite-preview', contents=prompt)
    is_safe = "UNSAFE" not in res.text.upper()
    return {"is_safe": is_safe, "final_response": state['draft_response']}

def check_safety(state: DebugState):
    if state["loop_count"] >= 3:
        return END # Force exit if stuck in a loop
    if state["is_safe"]:
        return END
    return "coach_node"

def debug_user_code(problem_id: str, user_code: str):
    session = SessionLocal()
    pe = session.get(ProblemEditorial, problem_id)
    session.close()

    if not pe or not pe.editorial_text:
        return {"error": "No official editorial available for this problem to use as a baseline."}

    # Initialize LangGraph Socratic Loop
    builder = StateGraph(DebugState)
    builder.add_node("critic_node", critic_node)
    builder.add_node("coach_node", coach_node)
    builder.add_node("reviewer_node", reviewer_node)
    
    builder.add_edge(START, "critic_node")
    builder.add_edge("critic_node", "coach_node")
    builder.add_edge("coach_node", "reviewer_node")
    builder.add_conditional_edges("reviewer_node", check_safety)
    
    graph = builder.compile()
    
    initial_state = {
        "user_code": user_code,
        "editorial": pe.editorial_text,
        "bug_report": "",
        "draft_response": "",
        "final_response": "",
        "loop_count": 0,
        "is_safe": True
    }
    
    try:
        final_state = graph.invoke(initial_state)
        return {"message": final_state["final_response"]}
    except Exception as e:
        return {"error": f"LangGraph pipeline failed: {e}"}

def parse_search_query(query: str):
    """Uses Gemini to extract semantic intent and metadata constraints from a raw query."""
    prompt = f"""
    You are an expert natural language parser for a competitive programming search engine.
    The user is searching for Codeforces problems. Extract the core semantic search topic and any constraints.
    
    User Query: "{query}"

    Extract the following fields into a strictly valid JSON object:
    - "semantic_query": The core algorithmic topic or concept to search for (e.g., "DP on trees", "String hashing"). Remove words like "Problems that have", "rating below", etc.
    - "max_rating": Integer or null. If the user specifies a rating below X or maximum rating X, put X here.
    - "min_rating": Integer or null. If the user specifies a rating above X or minimum rating X, put X here.
    - "required_tags": List of strings or empty list. If the user explicitly asks for specific algorithmic tags (e.g., "DP", "graphs", "greedy", "data structures"), list them here in lowercase.

    Example Query: "Problems that require DP and graphs to be solved and are rated below 2000"
    Output: {{"semantic_query": "DP and graphs", "max_rating": 2000, "min_rating": null, "required_tags": ["dp", "graphs"]}}

    Example Query: "Hard graph problems above rating 2500"
    Output: {{"semantic_query": "graph problems", "max_rating": null, "min_rating": 2500, "required_tags": ["graphs"]}}

    Example Query: "Segment tree lazy propagation"
    Output: {{"semantic_query": "Segment tree lazy propagation", "max_rating": null, "min_rating": null, "required_tags": []}}

    Return ONLY the raw JSON object, without any markdown formatting or backticks.
    """

    api_key_1 = os.getenv("GEMINI_API_KEY")
    client_present = genai.Client(api_key=api_key_1)
    
    try:
        res = safe_gemini_call(client=client_present, model='gemini-3.1-flash-lite-preview', contents=prompt)
        text = res.text.strip()
        if text.startswith("```json"):
            text = text[7:-3]
        elif text.startswith("```"):
            text = text[3:-3]
        
        data = json.loads(text.strip())
        return data
    except Exception as e:
        print(f"Error parsing query: {e}")
        return {"semantic_query": query, "max_rating": None, "min_rating": None}


def semantic_problem_search(query: str, top_k: int = 3):
    """Self-Querying Retriever: LLM Parsing -> Bi-Encoder (recall) -> Post-Filtering -> Cross-Encoder (precision)."""
    # 1. LLM Query Parsing
    parsed_query = parse_search_query(query)
    semantic_query = parsed_query.get("semantic_query", query)
    max_rating = parsed_query.get("max_rating")
    min_rating = parsed_query.get("min_rating")
    required_tags = parsed_query.get("required_tags", [])
    client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
    
    # Stage 1: Get top 50 from cf_editorials (High Recall)
    try:
        col_ed = client.get_collection(name="cf_editorials")
    except Exception:
        return {"error": "cf_editorials collection not found. Please run pipeline.py --step 4 first."}
        
    results = col_ed.query(query_texts=[semantic_query], n_results=50)
    
    if not results['ids'] or len(results['ids'][0]) == 0:
        return {"error": "No problems found matching the query."}
        
    pids = results['ids'][0]
    docs = results['documents'][0]
    
    # Stage 1.5: Fetch metadata & Post-Filtering
    try:
        col_prob = client.get_collection(name=CHROMA_COLLECTION_NAME)
        prob_details = col_prob.get(ids=pids)
    except Exception:
        return {"error": "cf_problems collection not found."}
        
    filtered_pids = []
    filtered_docs = []
    
    for i, pid in enumerate(pids):
        try:
            idx = prob_details['ids'].index(pid)
            meta = prob_details['metadatas'][idx]
            rating = meta.get("rating")
            tags = meta.get("tags", "").lower()
            
            # Apply strict tag constraints
            if required_tags:
                if not all(t in tags for t in required_tags):
                    continue
            
            # Apply rating constraints
            if rating is None or rating == "Unrated":
                if max_rating is not None or min_rating is not None:
                    continue 
            else:
                rating = int(rating)
                if max_rating is not None and rating > max_rating:
                    continue
                if min_rating is not None and rating < min_rating:
                    continue
                    
            filtered_pids.append(pid)
            filtered_docs.append(docs[i])
        except ValueError:
            pass # PID not in cf_problems
            
    if not filtered_pids:
        return {"error": "No problems found matching your constraints (e.g. rating limits)."}
        
    # Limit to top 15 remaining for Cross-Encoder speed
    filtered_pids = filtered_pids[:15]
    filtered_docs = filtered_docs[:15]
    
    # Stage 2: Cross-Encoder Re-ranking
    ce = get_cross_encoder()
    pairs = [[query, doc] for doc in filtered_docs] # Evaluate against the original query
    scores = ce.predict(pairs)
    
    # Sort by score descending
    scored_results = sorted(zip(scores, filtered_pids, filtered_docs), key=lambda x: x[0], reverse=True)
    
    # Take top_k
    top_results = scored_results[:top_k]
    top_pids = [item[1] for item in top_results]
    
    # Fetch problem details from cf_problems
    try:
        col_prob = client.get_collection(name=CHROMA_COLLECTION_NAME)
        prob_details = col_prob.get(ids=top_pids)
    except Exception:
        return {"error": "cf_problems collection not found."}
    
    final_problems = []
    # We must match the order of top_pids
    for pid in top_pids:
        try:
            idx = prob_details['ids'].index(pid)
            meta = prob_details['metadatas'][idx]
            final_problems.append({
                "problem_id": pid,
                "name": meta.get("name", "Unknown Problem"),
                "rating": meta.get("rating", "Unrated"),
                "tags": meta.get("tags", ""),
                "contest_id": meta.get("contest_id"),
                "index": meta.get("index"),
                "ce_score": float(scored_results[top_pids.index(pid)][0])
            })
        except ValueError:
            pass
            
    return {"results": final_problems}