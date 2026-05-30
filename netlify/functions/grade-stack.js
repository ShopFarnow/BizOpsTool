// netlify/functions/grade-stack.js
const https = require("https");
const { getStore } = require("@netlify/blobs");

const CACHE_TTL_MS = 24 * 60 * 60 * 1000;

const SYSTEM = `You are a BizOps stack expert. Analyze SaaS/open-source tool stacks and return structured JSON.
Focus on cost, vendor lock-in, open-source alternatives, and GitHub health scores.
Always return ONLY valid JSON, no markdown, no explanation.`;

function cacheKey(mode, tools) {
  const raw = mode + "|" + (typeof tools === "string" ? tools : JSON.stringify(tools));
  let h = 5381;
  for (let i = 0; i < raw.length; i++) h = ((h << 5) + h) ^ raw.charCodeAt(i);
  return "cache_" + mode + "_" + (h >>> 0).toString(36);
}

async function logRequest(store, mode, tools, output, ms, cached) {
  if (!store) return;
  try {
    const logKey = "log_" + Date.now() + "_" + Math.random().toString(36).slice(2, 7);
    await store.setJSON(logKey, {
      ts: new Date().toISOString(),
      mode,
      input: typeof tools === "string" ? tools : JSON.stringify(tools),
      output: typeof output === "string" ? output.slice(0, 500) : JSON.stringify(output).slice(0, 500),
      ms,
      cached,
    });
  } catch (e) {
    console.warn("Log write failed:", e.message);
  }
}

function openaiRequest(body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const req = https.request({
      hostname: "api.openai.com",
      path: "/v1/chat/completions",
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${process.env.OPENAI_API_KEY}`,
        "Content-Length": Buffer.byteLength(data),
      },
    }, (res) => {
      let raw = "";
      res.on("data", chunk => raw += chunk);
      res.on("end", () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(raw) }); }
        catch (e) { reject(new Error("Invalid JSON from OpenAI: " + raw.slice(0, 200))); }
      });
    });
    req.on("error", reject);
    req.write(data);
    req.end();
  });
}

exports.handler = async (event) => {
  const CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
  };

  if (event.httpMethod === "OPTIONS") return { statusCode: 204, headers: CORS, body: "" };
  if (event.httpMethod !== "POST") return { statusCode: 405, headers: CORS, body: "Method not allowed" };

  if (!process.env.OPENAI_API_KEY)
    return { statusCode: 500, headers: CORS, body: JSON.stringify({ error: "OPENAI_API_KEY not configured" }) };

  let payload;
  try { payload = JSON.parse(event.body || "{}"); }
  catch { return { statusCode: 400, headers: CORS, body: JSON.stringify({ error: "Invalid JSON body" }) }; }

  const { tools, mode, context } = payload;  // <-- FIXED: allow context for recommend mode
  if (!tools && mode !== "recommend") return { statusCode: 400, headers: CORS, body: JSON.stringify({ error: "tools required for grade/verdict" }) };

  let store;
  try {
    store = getStore({ name: "bizops-ai-log", consistency: "strong" });
  } catch (e) {
    console.warn("Blobs unavailable:", e.message);
    store = null;
  }

  const key = cacheKey(mode, tools || context);
  const start = Date.now();

  if (store) {
    try {
      const cached = await store.getWithMetadata(key, { type: "json" });
      if (cached?.data) {
        const age = Date.now() - (cached.metadata?.savedAt || 0);
        if (age < CACHE_TTL_MS) {
          console.log("Cache HIT:", key);
          await logRequest(store, mode, tools || context, cached.data.body, Date.now() - start, true);
          return {
            statusCode: 200,
            headers: { ...CORS, "Content-Type": "application/json", "X-Cache": "HIT" },
            body: cached.data.body,
          };
        }
      }
    } catch (e) {
      console.warn("Cache read failed:", e.message);
    }
  }

  const isVerdict = mode === "verdict";
  let prompt;

  if (mode === "recommend") {
    // context = { type, size, pains, tech }
    const ctx = context || {};
    prompt = buildRecommendPrompt(ctx);
  } else if (isVerdict) {
    prompt = buildVerdictPrompt(tools);
  } else {
    prompt = buildGradePrompt(tools);
  }

  try {
    const result = await openaiRequest({
      model: "gpt-4o-mini",
      max_tokens: 2000,
      temperature: 0.3,
      response_format: isVerdict ? undefined : { type: "json_object" },
      messages: [
        { role: "system", content: SYSTEM },
        { role: "user", content: prompt },
      ],
    });

    if (result.status !== 200) {
      return {
        statusCode: result.status,
        headers: CORS,
        body: JSON.stringify({ error: result.body?.error?.message || "OpenAI error" }),
      };
    }

    const text = result.body.choices?.[0]?.message?.content || (isVerdict ? "" : "{}");
    const responseBody = isVerdict ? JSON.stringify({ text }) : text;
    const ms = Date.now() - start;

    if (store) {
      try {
        await store.setJSON(key, { body: responseBody }, { metadata: { savedAt: Date.now(), mode, ms } });
      } catch (e) {
        console.warn("Cache write failed:", e.message);
      }
    }

    if (store) await logRequest(store, mode, tools || context, responseBody, ms, false);

    return {
      statusCode: 200,
      headers: { ...CORS, "Content-Type": "application/json", "X-Cache": "MISS" },
      body: responseBody,
    };
  } catch (err) {
    return { statusCode: 500, headers: CORS, body: JSON.stringify({ error: err.message }) };
  }
};

function buildGradePrompt(toolsList) {
  return `Analyze this BizOps tool stack: ${toolsList}

Return ONLY a JSON object:
{
  "score": <0-100 integer>,
  "grade": "<A+|A|B|C|D>",
  "verdict": "<one punchy sentence max 12 words>",
  "summary": "<2-sentence analysis of strengths and weaknesses>",
  "chips": ["<3-5 short tags e.g. High vendor lock-in, Good automation coverage>"],
  "tool_analysis": [
    { "name": "<tool name>", "status": "<ok|warn|bad>", "verdict": "<one sentence assessment>" }
  ],
  "recommendations": [
    {
      "name": "<open-source tool name>",
      "replaces": "<paid tool it replaces>",
      "category": "<category>",
      "description": "<why this tool, 1-2 sentences>",
      "bizopstool_slug": "<lowercase-hyphenated>",
      "github_url": "<real github url>",
      "monthly_saving_usd": <integer>,
      "bizops_score": <40-95>
    }
  ],
  "total_monthly_saving_usd": <integer>
}

Rules: score 85+=excellent, 70-84=good, 50-69=average, <50=poor.
Recommend 4-6 open-source replacements. Use real projects: n8n, NocoDB, Metabase, Supabase, Cal.com, PostHog, Plane, Chatwoot.
Salesforce ~$150/user, Tableau ~$70/user, Zapier ~$49/mo, Jira ~$10/user. Only return JSON.`;
}

function buildVerdictPrompt(toolList) {
  return `Write a crisp 3-paragraph comparison of these open-source business tools: ${toolList}.
First paragraph: what each tool does and who it's for.
Second paragraph: head-to-head verdict — which wins for which use case, based on their scores and activity.
Third paragraph: your recommendation — when to pick each one.
Be specific, opinionated, and practical. Write for a technical founder evaluating these tools today.
Return plain text only, no JSON, no markdown headers.`;
}

function buildRecommendPrompt(context) {
  const { type, size, pains, tech } = context;
  return `Recommend the BEST 6 open-source tools for: ${type} business, ${size} team, tech level "${tech}", pain points: ${(pains || []).join(', ')}.

Return ONLY a JSON object:
{
  "headline": "<10 word max headline for their stack>",
  "subtitle": "<2-sentence description of why this stack is right for them>",
  "tags": ["<3-4 tags like Self-hostable, No vendor lock-in>"],
  "tools": [
    {
      "name": "<tool name>", "category": "<CRM|Automation|Analytics|Database|DevOps|Collaboration|ERP>",
      "replaces": "<paid tool it replaces>", "why": "<1-2 sentences>",
      "slug": "<lowercase-hyphenated>", "github_url": "<real github url>",
      "bizops_score": <50-95>, "monthly_saving_usd": <integer>,
      "setup_difficulty": "<Easy|Medium|Hard>", "hosted_option": true
    }
  ],
  "total_monthly_saving_usd": <integer>,
  "migration_plan": [
    { "week": "Week 1",    "action": "<specific first step>" },
    { "week": "Week 2-3",  "action": "<second step>" },
    { "week": "Week 4",    "action": "<third step>" },
    { "week": "Month 2",   "action": "<longer term>" }
  ]
}

For "none" tech level only recommend tools with hosted/cloud options.
Use real tools: n8n, NocoDB, Metabase, Supabase, Cal.com, PostHog, Plane, Chatwoot, Mautic, AppFlowy, ERPNext.
Only return JSON.`;
}
