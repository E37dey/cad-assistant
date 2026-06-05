# Usage Notes & Lessons (from real testing)

Practical guidance distilled from building and stress‑testing CAD Assistant.

## ✍️ How to phrase prompts (this matters a LOT)

**Describe geometry + dimensions, not function.** The model builds far more
reliably from direct geometric wording than from functional/vague terms.

| ❌ Avoid (functional / vague) | ✅ Use (geometric / direct) |
|---|---|
| "bearing bore", "חור מיסב" | "30 mm hole centered, through", "חור 30 מ"מ במרכז דרכו" |
| "shoulder, chamfer in the bore" | (omit, or "step to Ø Y") |
| "mounting holes for bolts" | "4× 6 mm holes, 40 mm apart" |
| vague role description | exact size + position |

Words like *bore / מיסב / shoulder / כתף* (plus auto‑detail adding chamfers inside
a bore) push the AI toward a fragile cut‑extrude that fails with
*"No target body found to cut or intersect."* Plain geometry → reliable `holeFeatures`.

**For truly trivial parts** ("a cube"), turn OFF *פירוט אוטו'* — it over‑expands.
For anything non‑trivial, leave it ON.

## 🟢 What works well (the sweet spot)

- Prismatic parts: blocks, plates, discs, brackets.
- Holes / bores on the **top face**, counterbores, countersinks.
- **Circular patterns** — bolt circles, gear teeth.
- **Revolve** — stepped shafts, pulleys, anything turned.
- Fillets / chamfers on outer edges.

Verified clean: flange (bore + 6 bolt holes), spur gear, stepped shaft,
bearing block, mounting plate.

## 🟡 Current limits

- Holes on **side / angled faces** (e.g. the vertical wall of an L‑bracket) — often missing.
- **Slots / rectangular cuts** — can hit `InternalValidationError`.
- **2D drawings** — genuinely not possible via the Fusion API. Use the *📐 GD&T analysis*
  report instead, and Fusion's native **Drawing → From Design** for a real sheet.
- **Real threads / joints** — supported but finicky; for guaranteed results use Fusion's
  native **Modify → Thread** / **Assemble → Joint**.

**Workaround for tricky parts:** build the base body first, then use the **ערוך (Edit)** tab
to add features one at a time ("add a 30 mm hole through the center") — each feature then
targets an existing body.

## ⚙️ Operational reminders

- **Reload after any code change** — Stop + Run in *Utilities → Scripts and Add‑Ins* (Fusion
  caches the add‑in in memory).
- **Fusion Personal document limit (~10):** if you see a red **"Read‑Only: Document limit
  reached"** banner, clear old documents in the Data Panel — a read‑only doc silently
  corrupts builds. Check this FIRST when results look broken.
- **🧹 נקה מודל** between independent parts so they don't stack.

---

## 📣 LinkedIn post (EN + HE)

### English
> 🛠️ I built an AI assistant that turns plain language into real 3D CAD models — right inside Autodesk Fusion 360.
>
> You type "flange coupling with a 30 mm bore and 6 bolt holes" — in English or Hebrew — and it writes the Fusion 360 Python API code, runs it, and a real, dimensioned, parametric part appears in your viewport. Seconds, not hours.
>
> Under the hood: multi‑AI (Claude / Gemini / Groq / local Ollama), engineering‑grade feature trees with named features, manufacturing‑aware geometry (CNC / FDM / sheet‑metal), self‑verification that measures the built part against your request and auto‑corrects, real assemblies with joints that move, plus GD&T, material properties and STEP/STL export.
>
> The hard part wasn't the AI — it was making it reliable. LLMs write CAD API code that's often wrong on the first try, so I built guardrails: a sandbox, a code sanitizer, a self‑healing retry loop, and honest validation that refuses to report "success" unless the part is genuinely valid.
>
> Biggest lesson: knowing the boundary beats chasing perfection. Some things (programmatic 2D drawings) genuinely aren't exposed by the API — and being honest about that builds more trust than faking it.
>
> #CAD #Fusion360 #AI #MechanicalEngineering #Claude #BuildInPublic

### עברית
> 🛠️ בניתי אסיסטנט AI שהופך שפה רגילה למודלי CAD תלת‑ממדיים אמיתיים — בתוך Autodesk Fusion 360.
>
> אתה כותב "מצמד אוגן עם חור 30 מ"מ ו‑6 חורי בורג" — בעברית או אנגלית — והוא כותב את קוד ה‑Python API של Fusion, מריץ אותו, וחלק אמיתי, ממודד ופרמטרי מופיע ב‑viewport. שניות, לא שעות.
>
> מתחת למכסה: מולטי‑AI (Claude / Gemini / Groq / Ollama מקומי), feature tree ברמה הנדסית עם שמות, גיאומטריה מותאמת‑ייצור (CNC / הדפסת‑תלת / פח), אימות‑עצמי שמודד את החלק מול הבקשה ומתקן אוטומטית, הרכבות אמיתיות עם Joints שזזים, ועוד GD&T, תכונות חומר וייצוא STEP/STL.
>
> החלק הקשה לא היה ה‑AI — אלא להפוך אותו לאמין. מודלי שפה כותבים קוד CAD שלרוב שגוי בניסיון ראשון, אז בניתי מעקות: sandbox, מנקה‑קוד, לולאת תיקון‑עצמי, ו‑validation כן שמסרב לדווח "הצלחה" אלא אם החלק באמת תקין.
>
> הלקח הכי גדול: לדעת את הגבול מנצח רדיפה אחר מושלם. יש דברים (שרטוטי 2D תכנותית) שפשוט לא חשופים ב‑API — ולהיות כן לגבי זה בונה יותר אמון מלזייף.
>
> #CAD #Fusion360 #בינהמלאכותית #הנדסהמכנית #Claude
