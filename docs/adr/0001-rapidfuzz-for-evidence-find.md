# Use RapidFuzz for deterministic Evidence Find fallback

Session Index previously targeted Python stdlib-only runtime dependencies, but MIN-178 needs better deterministic near-match recall for Evidence Find topic lookup than the stdlib can provide without custom fuzzy-matching machinery. We will add `rapidfuzz` for bounded, deterministic fuzzy fallback over already-indexed session candidate text, while keeping model calls, embeddings, vector databases, and project-specific synonym registries out of the CLI retrieval path.
