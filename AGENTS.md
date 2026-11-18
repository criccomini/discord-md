## Coding Style

- You keep code simple. KISS is your motto.
- You ain't gonna need it. Embrace YAGNI.
- You keep the lines of code (LoC) in the codebase to an absolute minimum.
- You adhere to high-cohesion and low coupling design principles.
- You maintain a `docs` directory that reflects _the current state_ of the codebas.
- You're not a pack rat; you are comfortable deleting things. You have git to back you up.
- You only commit the absolute minimum number of tests to feel comfortable with your changes. Delete the rest before committing.
- If a small function is only called once, you can get rid of it.
- If a parameter always receives the same value, get rid of the parameter.
- You don't introduce any abstractions until they're actually used more than once.
- You don't worry about backwards/forwards API compatibility. LLMs are really good at refactoring now.
- You commit changes in small, reviewable chunks as you work.
- You do not litter the codebase with random worklogs, old milestones, or outdated notes.

## Writing Style

- Be concise. Write like a human. Avoid LLM slop-style writing, and LLM-isms.
- Use ASD-STE100 Simplified Technical English (STE) when writing.
- You never speak in jargon. Keep _all_ jargon out of the codebase.
- You leave clear, concise code comments using standard idiomatic notation.
- You only write in active voicel--never passive.
