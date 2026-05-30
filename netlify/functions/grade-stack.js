// netlify/functions/grade-stack.js
// Deploy on Netlify — set OPENAI_API_KEY in environment variables
// Users never see the key; all AI calls go through this function

const https = require("https");

const SYSTEM = `You are a BizOps stack expert. Analyze SaaS/open-source tool stacks and return structured JSON.
Focus on cost, vendor lock-in, open-source alternatives, and GitHub health scores.
Always return ONLY valid JSON, no markdown, no explanation.`;

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

  if (!process.env.OPENAI_API_KEY) {
    return { statusCode: 500, headers: CORS, body: JSON.stringify({ error: "OPENAI_API_KEY not configured" }) };
  }

  let payload;
  try { payload = JSON.parse(event.body || "{}"); }
  catch { return { statusCode: 400, headers: CORS, body: JSON.stringify({ error: "Invalid JSON body" }) }; }

  const { tools, mode } = payload; // mode: "grade" | "recommend"
  if (!tools) return { statusCode: 400, headers: CORS, body: JSON.stringify({ error: "tools required" }) };

  const isRecommend = mode === "recommend";
  const prompt = isRecommend
    ? buildRecommendPrompt(tools)
    : buildGradePrompt(tools);

  try {
    const result = await openaiRequest({
      model: "gpt-4o-mini",
      max_tokens: 2000,
      temperature: 0.3,
      response_format: { type: "json_object" },
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

    const text = result.body.choices?.[0]?.message?.content || "{}";
    return { statusCode: 200, headers: { ...CORS, "Content-Type": "application/json" }, body: text };

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

function buildRecommendPrompt(context) {
  const { type, size, pains, tech } = context;
  return `Recommend the BEST 6 open-source tools for: ${type} business, ${size} team, tech level "${tech}", pain points: ${(pains||[]).join(', ')}.

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
    { "week": "Week 1", "action": "<specific first step>" },
    { "week": "Week 2-3", "action": "<second step>" },
    { "week": "Week 4", "action": "<third step>" },
    { "week": "Month 2", "action": "<longer term>" }
  ]
}

For "none" tech level only recommend tools with hosted/cloud options.
Use real tools: n8n, NocoDB, Metabase, Supabase, Cal.com, PostHog, Plane, Chatwoot, Mautic, AppFlowy, ERPNext.
Only return JSON.`;
}
