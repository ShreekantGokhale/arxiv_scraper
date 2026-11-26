"""
Keyword search over titles and abstracts with MySQL's FULLTEXT index.

MySQL's boolean full-text mode has traps for raw user input, so queries are rebuilt:
  * a required word shorter than 3 letters, or a stopword ("the", "for"), matches
    nothing, so those words are dropped unless they sit inside a phrase;
  * an unquoted hyphen means NOT ("retrieval-augmented" would exclude "augmented"),
    so hyphenated words become phrases;
  * operator characters (+ - < > ( ) ~ * @) in user input are removed.
"""
from __future__ import annotations

import re
from enum import Enum

# InnoDB's default stopword list (INFORMATION_SCHEMA.INNODB_FT_DEFAULT_STOPWORD).
STOPWORDS = {
    "a", "about", "an", "are", "as", "at", "be", "by", "com", "de", "en", "for", "from",
    "how", "i", "in", "is", "it", "la", "of", "on", "or", "that", "the", "this", "to",
    "was", "what", "when", "where", "who", "will", "with", "und", "www",
}
MIN_WORD_LENGTH = 3  # innodb_ft_min_token_size default

# Topic shortcuts: any of these phrases/words matches (boolean-mode OR).
# GenAI and RAG are research topics, not arXiv categories, so they're searched as text.
TOPICS: dict[str, dict[str, str]] = {
    "rag": {
        "label": "Retrieval-augmented generation",
        "query": '"retrieval-augmented" "retrieval augmented" "retrieval augmentation" RAG',
    },
    "llm": {
        "label": "Large language models",
        "query": '"large language model" "large language models" "language model" LLM LLMs',
    },
    "genai": {
        "label": "Generative AI",
        "query": '"generative AI" "generative model" "generative models" "text-to-image" '
                 '"text-to-video" "image generation" "video generation" diffusion',
    },
    "agents": {
        "label": "AI agents and multi-agent systems",
        "query": '"LLM agent" "LLM agents" "language agent" "language agents" "multi-agent" '
                 'agentic "autonomous agent" "autonomous agents"',
    },
    "prompting": {
        "label": "Prompting and in-context learning",
        "query": '"prompt engineering" prompting "in-context learning" "chain-of-thought"',
    },
    "hallucination": {
        "label": "Hallucination and factuality",
        "query": 'hallucination hallucinations factuality "factual consistency"',
    },
    "human_ai": {
        "label": "Human-AI interaction",
        "query": '"human-AI" "human-centered AI" "human-in-the-loop" "user study" "user studies" '
                 '"human-computer interaction" usability',
    },
}

TopicName = Enum("TopicName", {key: key for key in TOPICS}, type=str)

_TOKEN_RE = re.compile(r'"([^"]+)"|(\S+)')
_WORD_RE = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)  # letters/digits, inner hyphens


def build_boolean_query(user_query: str) -> str | None:
    """
    Turn free text into a MySQL boolean-mode query where every term is required.

        'rag "knowledge graph" medical'  ->  '+rag* +"knowledge graph" +medical*'
        'retrieval-augmented agents'     ->  '+"retrieval augmented" +agents*'

    Returns None when nothing searchable is left (e.g. only "AI" or "the").
    """
    terms: list[str] = []
    for phrase, word in _TOKEN_RE.findall(user_query):
        if phrase:
            cleaned = " ".join(_WORD_RE.findall(phrase)).replace("-", " ")
            if cleaned:
                terms.append(f'+"{cleaned}"')
            continue
        for w in _WORD_RE.findall(word):
            if "-" in w:
                terms.append(f'+"{w.replace("-", " ")}"')
            elif len(w) >= MIN_WORD_LENGTH and w.lower() not in STOPWORDS:
                terms.append(f"+{w}*")  # trailing * also matches plurals: agent -> agents
    return " ".join(dict.fromkeys(terms)) or None
