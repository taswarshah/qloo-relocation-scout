import asyncio, json
from agent import scout
P = {
 "indie_creative": {"music":"Radiohead, Bon Iver","films":"Amelie, Lost in Translation","food":"Blue Bottle Coffee","lifestyle":"art, cafes, walkable"},
 "foodie_nightlife": {"music":"Daft Punk, Bad Bunny","films":"The Bear","food":"Nobu","lifestyle":"nightlife, beaches"},
 "outdoorsy_family": {"music":"Coldplay, Taylor Swift","films":"Up, Paddington","lifestyle":"hiking, kids, nature","country":"US"},
}
async def main():
    for n,p in P.items():
        r = await scout(p, 3)
        print("=====",n, "| matched:", [m["name"] for m in r.get("matched",[])], "| tags:", [t["id"] for t in r.get("lifestyle_tags",[])])
        if "error" in r: print(r); continue
        for c in r["cities"]: print(" *", c["location"], "|", [h["name"] for h in c["neighborhoods"]], "\n   ", c["why"])
asyncio.run(main())
