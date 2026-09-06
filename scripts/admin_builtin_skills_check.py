import asyncio
import json

from app.services.visibility import builtin_skills_overview

print(json.dumps(asyncio.run(builtin_skills_overview()), ensure_ascii=False, indent=2))
