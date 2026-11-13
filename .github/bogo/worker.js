// 보고프구 월드 데이터 관리 페이지(Cloudflare Worker)
// 비밀번호로 들어와 관리자 명단과 액자 사진을 바꾼다. 바꾸면 bogo-data 저장소에 커밋되고, 저장소의 자동 반영이 1~2분 뒤 월드가 받는 주소에 올린다
// 이 Worker의 Settings → Variables and Secrets에 비밀 값 두 개가 있어야 한다
//   GITHUB_TOKEN   : bogo-data 저장소의 Contents 읽기·쓰기 토큰
//   ADMIN_PASSWORD : 이 페이지 비밀번호. 바꾸면 들어와 있던 사람도 모두 새 비밀번호를 넣어야 한다

const REPO = "MissingE/bogo-data", BRANCH = "main", SITE = "https://missinge.github.io/bogo-data/";
const COOKIE = "bogo_s", DAYS = 30, MAX_B64 = 16 * 1024 * 1024, MAX_NAMES = 300;
const ADMIN_HEAD = "# 한 줄에 한 명씩 VRChat 닉네임을 적으세요. '#'으로 시작하는 줄은 무시됩니다.\n";
const enc = new TextEncoder();

export default {
  async fetch(req, env)
  {
    const path = new URL(req.url).pathname, method = req.method;
    try
    {
      if (!env.GITHUB_TOKEN || !env.ADMIN_PASSWORD)
      {
        const missing = ["GITHUB_TOKEN", "ADMIN_PASSWORD"].filter((k) => !env[k]).join(", ");
        return page(SETUP_PAGE.replace("<!--missing-->", missing), 503);
      }
      if (path === "/login")
      {
        return method === "POST" ? await login(req, env) : new Response(null, { status: 303, headers: { Location: "/" } });
      }
      if (path === "/logout")
      {
        return new Response(null, { status: 303, headers: { Location: "/", "Set-Cookie": cookie("", 0) } });
      }
      if (!(await session(req, env)))
      {
        return path.startsWith("/api/") ? json({ error: "다시 로그인해 주세요" }, 401) : page(loginPage(""));
      }
      if (path === "/" && method === "GET")
      {
        return page(APP_PAGE);
      }
      if (path === "/api/state" && method === "GET")
      {
        return await state(env);
      }
      if (path === "/api/admins" && method === "PUT")
      {
        return await saveAdmins(req, env);
      }
      const photo = path.match(/^\/api\/photo\/(\d+)$/), reset = path.match(/^\/api\/reset\/(\d+)$/);
      if (photo && method === "PUT")
      {
        return await savePhoto(req, env, photo[1]);
      }
      if (reset && method === "POST")
      {
        return await resetPhoto(env, reset[1]);
      }
      return new Response("Not found", { status: 404 });
    }
    catch (e)
    {
      return json({ error: String((e && e.message) || e) }, 500);
    }
  },
};

// ---------------------------------------------------------------- 로그인: 비밀번호로 서명한 만료 시각을 쿠키로 30일 쓴다(서버에 저장하는 것은 없음)
function cookie(value, age)
{
  return COOKIE + "=" + value + "; Path=/; Max-Age=" + age + "; HttpOnly; Secure; SameSite=Lax";
}

async function hmacKey(env)
{
  return crypto.subtle.importKey("raw", enc.encode("bogo:" + env.ADMIN_PASSWORD), { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
}

function b64url(buf)
{
  return b64(new Uint8Array(buf)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function unb64url(s)
{
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(s + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0));
}

async function session(req, env)
{
  const raw = (req.headers.get("Cookie") || "").split(/;\s*/).find((s) => s.startsWith(COOKIE + "="));
  if (!raw)
  {
    return false;
  }
  const v = raw.slice(COOKIE.length + 1), dot = v.indexOf(".");
  if (dot < 1)
  {
    return false;
  }
  try
  {
    const until = v.slice(0, dot);
    return Number(until) > Date.now() && (await crypto.subtle.verify("HMAC", await hmacKey(env), unb64url(v.slice(dot + 1)), enc.encode(until)));
  }
  catch
  {
    return false;
  }
}

async function login(req, env)
{
  const pw = String((await req.formData()).get("password") || ""), key = await hmacKey(env);
  // 두 값의 서명을 끝까지 비교해서 몇 글자가 맞았는지 시간으로 새지 않게 한다
  const a = new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(pw))), b = new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(env.ADMIN_PASSWORD)));
  let diff = 0;
  for (let i = 0; i < a.length; i++)
  {
    diff |= a[i] ^ b[i];
  }
  if (diff !== 0)
  {
    await new Promise((r) => setTimeout(r, 800));
    return page(loginPage("비밀번호가 맞지 않습니다"), 401);
  }
  const until = String(Date.now() + DAYS * 864e5), sig = await crypto.subtle.sign("HMAC", key, enc.encode(until));
  return new Response(null, { status: 303, headers: { Location: "/", "Set-Cookie": cookie(until + "." + b64url(sig), DAYS * 86400) } });
}

// ---------------------------------------------------------------- GitHub 저장소 읽기·쓰기
async function gh(env, path, init = {})
{
  const r = await fetch("https://api.github.com/repos/" + REPO + path, { ...init, headers: { Authorization: "Bearer " + env.GITHUB_TOKEN, Accept: "application/vnd.github+json", "User-Agent": "bogo-admin", "X-GitHub-Api-Version": "2022-11-28", ...(init.headers || {}) } });
  if (r.status === 401 || r.status === 403)
  {
    throw new Error("저장소 토큰(GITHUB_TOKEN)이 맞지 않거나 권한이 없습니다. 토큰의 Contents가 Read and write인지 확인해 주세요");
  }
  return r;
}

async function fail(r)
{
  const j = await r.json().catch(() => ({}));
  return new Error("저장하지 못했습니다 (GitHub " + r.status + (j.message ? ": " + j.message : "") + ")");
}

async function getFile(env, path)
{
  const r = await gh(env, "/contents/" + path + "?ref=" + BRANCH);
  if (r.status === 404)
  {
    return null;
  }
  if (!r.ok)
  {
    throw await fail(r);
  }
  return r.json();
}

// 사진 파일 내용까지 받지 않도록 폴더 목록에서 파일별 sha만 읽는다
async function shas(env, dir)
{
  const r = await gh(env, "/contents/" + dir + "?ref=" + BRANCH);
  if (r.status === 404)
  {
    return {};
  }
  if (!r.ok)
  {
    throw await fail(r);
  }
  const out = {};
  for (const f of await r.json())
  {
    out[f.name] = f.sha;
  }
  return out;
}

// 큰 사진도 무료 요금제 CPU 한도(요청당 10ms) 안에 들도록 base64 글자를 다시 훑지 않고 이어 붙인다
function putBody(message, content, sha)
{
  return '{"message":' + JSON.stringify(message) + ',"branch":"' + BRANCH + '"' + (sha ? ',"sha":"' + sha + '"' : "") + ',"content":"' + content + '"}';
}

// 같은 액자를 동시에 바꾸면 GitHub가 sha가 다르다고 거절하므로 최신 sha로 두 번 더 시도한다(나중에 올린 사진이 남는다)
async function putImage(env, n, content, message)
{
  for (let i = 0; i < 3; i++)
  {
    const sha = (await shas(env, "images"))[n + ".jpg"];
    const r = await gh(env, "/contents/images/" + n + ".jpg", { method: "PUT", headers: { "Content-Type": "application/json" }, body: putBody(message, content, sha) });
    if (r.ok)
    {
      return;
    }
    const e = await fail(r);
    if (r.status !== 409 && !(r.status === 422 && /sha/i.test(e.message)))
    {
      throw e;
    }
  }
  throw new Error("다른 사람이 같은 액자를 바꾸는 중입니다. 잠시 뒤 다시 해 주세요");
}

function b64(a)
{
  let s = "";
  for (let i = 0; i < a.length; i += 0x8000)
  {
    s += String.fromCharCode.apply(null, a.subarray(i, i + 0x8000));
  }
  return btoa(s);
}

function text64(s)
{
  return new TextDecoder().decode(Uint8Array.from(atob(s.replace(/\s/g, "")), (c) => c.charCodeAt(0))).replace(/^﻿/, "").replace(/\r/g, "");
}

let frameCache = null;

async function frames(env)
{
  if (frameCache)
  {
    return frameCache;
  }
  const f = await getFile(env, ".github/bogo/slots.json");
  if (!f)
  {
    throw new Error("저장소를 찾지 못했습니다. 토큰을 만들 때 bogo-data 저장소를 골랐는지 확인해 주세요");
  }
  const out = [];
  for (const s of JSON.parse(text64(f.content)).slots)
  {
    const grid = s.grid || [1, 1], w = s.w / grid[0], h = s.h / grid[1];
    const shape = w > h * 1.1 ? "가로 사진" : h > w * 1.1 ? "세로 사진" : "정사각형 사진";
    for (const n of s.parts || [s.name])
    {
      out.push({ n, label: s.label, shape, ratio: Math.round((w / h) * 10000) / 10000, w: Math.round(w), h: Math.round(h) });
    }
  }
  frameCache = out;
  return out;
}

async function known(env, n)
{
  return (await frames(env)).some((f) => f.n === n);
}

// ---------------------------------------------------------------- 페이지가 부르는 곳
async function state(env)
{
  const [list, admins] = await Promise.all([frames(env), getFile(env, "admins.txt")]);
  const names = admins ? text64(admins.content).split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("#")) : [];
  return json({ frames: list, admins: names, adminsSha: admins ? admins.sha : "", site: SITE });
}

async function saveAdmins(req, env)
{
  const body = await req.json(), seen = new Set(), names = [];
  for (const raw of Array.isArray(body.names) ? body.names : [])
  {
    const name = String(raw).replace(/[\r\n]/g, "").trim().slice(0, 100);
    if (name && !name.startsWith("#") && !seen.has(name.toLowerCase()) && names.length < MAX_NAMES)
    {
      seen.add(name.toLowerCase());
      names.push(name);
    }
  }
  const put = { message: "관리자 명단 수정", branch: BRANCH, content: b64(enc.encode(ADMIN_HEAD + names.join("\n") + "\n")) };
  if (body.sha)
  {
    put.sha = String(body.sha);
  }
  // 페이지를 연 뒤 다른 사람이 명단을 바꿨으면 덮어쓰지 않고 새로고침하게 한다
  const r = await gh(env, "/contents/admins.txt", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(put) });
  if (r.status === 409 || r.status === 422)
  {
    return json({ error: "그 사이 다른 사람이 명단을 바꿨습니다. 새로고침한 뒤 다시 고쳐 주세요" }, 409);
  }
  if (!r.ok)
  {
    throw await fail(r);
  }
  return json({ ok: true, count: names.length, sha: (await r.json()).content.sha });
}

// 사진은 브라우저에서 jpg로 줄이고 base64로 바꿔 보낸다(사진 속 위치 정보 같은 메타데이터도 이때 빠진다)
async function savePhoto(req, env, n)
{
  if (!(await known(env, n)))
  {
    return json({ error: n + "번 액자는 없습니다" }, 400);
  }
  if (Number(req.headers.get("Content-Length") || 0) > MAX_B64)
  {
    return json({ error: "사진이 너무 큽니다" }, 413);
  }
  const data = await req.text();
  if (data.length < 200 || data.length > MAX_B64 || !data.startsWith("/9j/") || data.indexOf('"') >= 0 || data.indexOf("\\") >= 0 || data.indexOf("\n") >= 0)
  {
    return json({ error: "사진을 읽지 못했습니다. 다른 사진으로 해 주세요" }, 400);
  }
  await putImage(env, n, data, n + "번 액자 사진 바꿈");
  return json({ ok: true });
}

async function resetPhoto(env, n)
{
  if (!(await known(env, n)))
  {
    return json({ error: n + "번 액자는 없습니다" }, 400);
  }
  const src = await getFile(env, ".github/bogo/numbers/" + n + ".jpg");
  if (!src)
  {
    return json({ error: "번호 그림을 찾지 못했습니다" }, 404);
  }
  const content = src.content.replace(/\s/g, "");
  await putImage(env, n, content, n + "번 액자를 번호 그림으로 되돌림");
  return json({ ok: true, preview: content });
}

// ---------------------------------------------------------------- 응답
const SITE_ORIGIN = new URL(SITE).origin;
const CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' " + SITE_ORIGIN + " blob: data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'";

function json(obj, status = 200)
{
  return new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" } });
}

function page(body, status = 200)
{
  return new Response(body, { status, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": CSP } });
}

const HEAD = `<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><meta name="referrer" content="no-referrer"><title>보고프구 월드 데이터 관리</title>
<style>
:root{--bg:#fff6f8;--card:#fff;--ink:#3a2230;--soft:#8a6070;--pink:#e0457b;--line:#f1d3dc}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,"Malgun Gothic","Apple SD Gothic Neo",sans-serif;background:var(--bg);color:var(--ink)}
.wrap{max-width:1100px;margin:auto;padding:16px}h1{font-size:22px;margin:4px 0 6px}h2{font-size:17px;margin:22px 0 8px}
.box{background:var(--card);border-radius:12px;padding:14px;box-shadow:0 1px 4px #0001}
.btn{display:inline-block;border:0;background:var(--pink);color:#fff;border-radius:8px;padding:9px 14px;font-size:14px;cursor:pointer;text-decoration:none;font-family:inherit}
.btn.sub{background:#fff;color:#c2336a;border:1px solid var(--pink)}.btn:disabled{opacity:.5;cursor:default}
textarea{width:100%;min-height:150px;font-size:15px;line-height:1.5;padding:10px;border:1px solid var(--line);border-radius:8px;font-family:inherit;resize:vertical}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:8px}.msg{font-size:13px;color:var(--soft);line-height:1.5}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border-radius:12px;padding:10px;box-shadow:0 1px 4px #0001;display:flex;flex-direction:column;gap:6px;border:2px solid transparent}
.card.over{border-color:var(--pink)}.pic{height:130px;background:#f9e2dc;border-radius:8px;display:flex;align-items:center;justify-content:center;overflow:hidden}
.pic img{display:block;height:auto;object-fit:cover;background:#fff}
.num{font-weight:700;font-size:18px}.where{font-size:13px;color:var(--soft)}.size{font-size:12px;color:#c2336a}.card .msg{min-height:18px}
.sizes{margin-bottom:14px}.sizes summary{cursor:pointer;font-weight:600;font-size:15px}.sizes table{width:100%;border-collapse:collapse;font-size:14px;margin-top:8px}
.sizes th,.sizes td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}.sizes th{color:var(--soft);font-weight:600;font-size:13px}.sizes td.px{white-space:nowrap;font-weight:600}
.reset{font-size:12px;color:var(--soft);background:none;border:0;cursor:pointer;text-decoration:underline;padding:0;align-self:flex-start;font-family:inherit}
.top{display:flex;justify-content:space-between;align-items:center;gap:8px}.err{color:#c0244f;font-size:14px}
input[type=password]{font-size:16px;padding:10px;border:1px solid var(--line);border-radius:8px;width:100%;margin:10px 0}
</style>`;

function loginPage(error)
{
  return HEAD + `<div class="wrap" style="max-width:380px;margin-top:12vh"><div class="box"><h1>보고프구 월드 데이터 관리</h1>
<form method="post" action="/login"><input type="password" name="password" placeholder="비밀번호" autocomplete="current-password" autofocus required>` + (error ? `<p class="err">${error}</p>` : "") + `<button class="btn" style="width:100%">들어가기</button></form></div></div></html>`;
}

const SETUP_PAGE = HEAD + `<div class="wrap" style="max-width:560px;margin-top:10vh"><div class="box"><h1>설정이 아직 덜 됐습니다</h1>
<p class="msg">Cloudflare에서 이 Worker의 <b>Settings → Variables and Secrets</b>에 <b><!--missing--></b> 비밀 값을 넣고 <b>Deploy</b>를 눌러 주세요.</p></div></div></html>`;

const APP_PAGE = HEAD + `<div class="wrap"><div class="top"><h1>보고프구 월드 데이터 관리</h1><a class="btn sub" href="/logout">나가기</a></div>
<p class="msg">바꾼 내용은 1~2분 뒤 월드에 반영됩니다. 월드를 다시 올릴 필요는 없습니다.</p>
<h2>관리자 명단</h2><div class="box"><p class="msg" style="margin-top:0">한 줄에 한 명씩 VRChat 닉네임을 띄어쓰기까지 똑같이 적고 <b>저장</b>을 누르세요. 그 사람 머리 위에 '관리자'가 뜹니다. 빼려면 그 줄을 지우고 저장합니다.</p>
<textarea id="admins" spellcheck="false" placeholder="불러오는 중"></textarea><div class="row"><button class="btn" id="saveAdmins">저장</button><span class="msg" id="adminsMsg"></span></div></div>
<h2>액자 사진</h2><p class="msg">월드의 액자에 적힌 번호를 찾아 <b>사진 바꾸기</b>를 누르고 내 컴퓨터의 사진을 고르세요(사진을 칸에 끌어다 놓아도 됩니다). 칸 비율에 맞게 가운데 기준으로 잘립니다.</p>
<details class="box sizes" open><summary>사진 크기 안내</summary><table><thead><tr><th>위치</th><th>액자 번호</th><th>모양</th><th>권장 크기(픽셀)</th></tr></thead><tbody id="sizes"></tbody></table>
<p class="msg" style="margin-bottom:0">권장 크기 이상이면 충분합니다. 더 큰 사진은 자동으로 줄이고, 비율이 다르면 가운데 기준으로 잘립니다. 권장 크기보다 많이 작으면 흐리게 보일 수 있습니다.</p></details>
<div class="grid" id="grid"></div></div>
<input type="file" id="pick" accept="image/*" hidden>
<script>
const $ = (s) => document.querySelector(s);
let site = "", adminsSha = "", target = null;
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
// 흔한 비율이면 그 이름으로(포스터는 A 용지 비율), 아니면 가로:세로 그대로
function ratioText(w, h)
{
  const named = [[1, 1, "1:1"], [16, 9, "16:9"], [9, 16, "9:16"], [3, 2, "3:2"], [2, 3, "2:3"], [4, 3, "4:3"], [3, 4, "3:4"], [1, 1.4142, "1:1.41 (A 용지)"], [1.4142, 1, "1.41:1 (A 용지)"]];
  for (const [a, b, name] of named)
  {
    if (Math.abs(Math.log((w / h) / (a / b))) < 0.02)
    {
      return name;
    }
  }
  return w + ":" + h;
}
async function call(path, opts)
{
  const r = await fetch(path, opts);
  if (r.status === 401)
  {
    location.href = "/";
    throw new Error("다시 로그인해 주세요");
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok)
  {
    throw new Error(j.error || "오류가 났습니다 (" + r.status + ")");
  }
  return j;
}
async function load()
{
  const s = await call("/api/state");
  site = s.site; adminsSha = s.adminsSha;
  $("#admins").value = s.admins.join("\\n"); $("#admins").placeholder = "예) 보고프구관리자";
  const t = Math.floor(Date.now() / 60000);
  // 크기 안내: 이어진 번호 중 위치와 크기가 같은 칸을 한 줄로 묶는다(1~3번 방처럼 크기만 같은 방도 묶는다)
  const rows = [], room = /^(\\d+)번 방$/;
  for (const f of s.frames)
  {
    const last = rows[rows.length - 1];
    if (last && last.w === f.w && last.h === f.h && (last.label === f.label || (room.test(last.label) && room.test(f.label))))
    {
      last.to = f.n; last.last = f.label;
    }
    else
    {
      rows.push({ label: f.label, last: f.label, from: f.n, to: f.n, w: f.w, h: f.h, shape: f.shape });
    }
  }
  $("#sizes").innerHTML = rows.map((r) =>
  {
    const where = r.last !== r.label ? r.label.match(room)[1] + "~" + r.last.match(room)[1] + "번 방" : r.label;
    return "<tr><td>" + esc(where) + "</td><td>" + esc(r.from === r.to ? r.from + "번" : r.from + "~" + r.to + "번") + "</td><td>" + esc(r.shape.replace(" 사진", "") + " " + ratioText(r.w, r.h)) + '</td><td class="px">' + r.w + " × " + r.h + "</td></tr>";
  }).join("");
  // 그림 칸을 액자 비율로 잘라 보여 줘서 월드에서 어디가 잘릴지 미리 보이게 한다
  $("#grid").innerHTML = s.frames.map((f) => '<div class="card" data-n="' + esc(f.n) + '"><div class="pic"><img style="aspect-ratio:' + Number(f.ratio) + ";width:min(100%," + Math.round(130 * f.ratio) + 'px)" src="' + site + "thumbs/" + esc(f.n) + ".jpg?t=" + t + '" alt=""></div><div class="num">' + esc(f.n) + '번</div><div class="where">' + esc(f.label) + " · " + esc(f.shape) + '</div><div class="size">권장 ' + f.w + " × " + f.h + " · " + ratioText(f.w, f.h) + '</div><button class="btn pick">사진 바꾸기</button><button class="reset">번호 그림으로 되돌리기</button><div class="msg"></div></div>').join("");
  for (const img of document.querySelectorAll(".card img"))
  {
    img.onerror = () => { img.style.visibility = "hidden"; };
  }
}
$("#saveAdmins").onclick = async () =>
{
  const b = $("#saveAdmins"); b.disabled = true; $("#adminsMsg").textContent = "저장하는 중";
  try
  {
    const names = $("#admins").value.split("\\n").map((x) => x.trim()).filter(Boolean);
    const j = await call("/api/admins", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ names, sha: adminsSha }) });
    adminsSha = j.sha;
    $("#adminsMsg").textContent = j.count + "명 저장했습니다. 1~2분 뒤 월드에 반영됩니다.";
  }
  catch (e)
  {
    $("#adminsMsg").textContent = e.message;
  }
  b.disabled = false;
};
// 긴 쪽 2048px 안으로 줄여 jpg로 다시 만든다(휴대폰 사진의 회전 정보는 반영)
async function shrink(file)
{
  const bmp = await createImageBitmap(file, { imageOrientation: "from-image" }).catch(() => createImageBitmap(file)).catch(() => null);
  if (!bmp)
  {
    throw new Error("이 사진은 열 수 없습니다. jpg나 png로 해 주세요");
  }
  const k = Math.min(1, 2048 / Math.max(bmp.width, bmp.height)), c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(bmp.width * k)); c.height = Math.max(1, Math.round(bmp.height * k));
  const g = c.getContext("2d"); g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height); g.drawImage(bmp, 0, 0, c.width, c.height);
  return new Promise((ok, no) => c.toBlob((b) => (b ? ok(b) : no(new Error("사진을 바꾸지 못했습니다"))), "image/jpeg", 0.9));
}
function base64(blob)
{
  return new Promise((ok, no) => { const fr = new FileReader(); fr.onload = () => ok(String(fr.result).split(",")[1]); fr.onerror = () => no(new Error("사진을 읽지 못했습니다")); fr.readAsDataURL(blob); });
}
async function upload(card, file)
{
  const msg = card.querySelector(".msg"), btn = card.querySelector(".pick"), img = card.querySelector("img");
  btn.disabled = true; msg.textContent = "올리는 중";
  try
  {
    const blob = await shrink(file);
    await call("/api/photo/" + card.dataset.n, { method: "PUT", headers: { "Content-Type": "text/plain" }, body: await base64(blob) });
    img.src = URL.createObjectURL(blob); img.style.visibility = "";
    msg.textContent = "바꿨습니다. 1~2분 뒤 월드에 반영됩니다.";
  }
  catch (e)
  {
    msg.textContent = e.message;
  }
  btn.disabled = false;
}
$("#grid").onclick = async (ev) =>
{
  const card = ev.target.closest(".card");
  if (!card)
  {
    return;
  }
  if (ev.target.classList.contains("pick"))
  {
    target = card; $("#pick").value = ""; $("#pick").click();
  }
  if (ev.target.classList.contains("reset") && confirm(card.dataset.n + "번 액자를 번호 그림으로 되돌릴까요?"))
  {
    const msg = card.querySelector(".msg"), img = card.querySelector("img");
    msg.textContent = "되돌리는 중";
    try
    {
      const j = await call("/api/reset/" + card.dataset.n, { method: "POST" });
      img.src = "data:image/jpeg;base64," + j.preview; img.style.visibility = "";
      msg.textContent = "되돌렸습니다. 1~2분 뒤 월드에 반영됩니다.";
    }
    catch (e)
    {
      msg.textContent = e.message;
    }
  }
};
$("#pick").onchange = () =>
{
  if ($("#pick").files[0] && target)
  {
    upload(target, $("#pick").files[0]);
  }
};
// 사진을 칸에 끌어다 놓아도 바꾼다. 칸 밖에 놓으면 브라우저가 사진을 열어 버리지 않게 막는다
addEventListener("dragover", (ev) =>
{
  ev.preventDefault();
  for (const c of document.querySelectorAll(".card.over"))
  {
    c.classList.remove("over");
  }
  const card = ev.target.closest && ev.target.closest(".card");
  if (card)
  {
    card.classList.add("over");
  }
});
addEventListener("drop", (ev) =>
{
  ev.preventDefault();
  const card = ev.target.closest && ev.target.closest(".card");
  for (const c of document.querySelectorAll(".card.over"))
  {
    c.classList.remove("over");
  }
  if (card && ev.dataTransfer.files[0])
  {
    upload(card, ev.dataTransfer.files[0]);
  }
});
load().catch((e) => { $("#grid").textContent = e.message; });
</script></html>`;
