from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from agent import scout

app = FastAPI(title="Relocation Scout")
class Profile(BaseModel):
    music: str = ""; films: str = ""; tv: str = ""; books: str = ""
    food: str = ""; brands: str = ""; lifestyle: str = ""; country: str = ""

@app.post("/api/scout")
async def api_scout(p: Profile): return await scout(p.model_dump())
@app.get("/healthz")
def health(): return {"ok": True}
app.mount("/static", StaticFiles(directory="static"), name="static")
@app.get("/")
def index(): return FileResponse("static/index.html")
