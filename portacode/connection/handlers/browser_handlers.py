"""Bounded Playwright browser automation for Dashboard AI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict

from .base import AsyncHandler
from .runtime_user import get_default_runtime_user, get_runtime_user_home, mkdir_with_owner, wrap_argv_for_user


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_WORKERS: dict[tuple[str, str], "_BrowserWorker"] = {}
_WORKERS_LOCK: asyncio.Lock | None = None
_NODE_PATH: str | None = None
_NODE_PATH_LOCK: asyncio.Lock | None = None
_WORKER_IDLE_SECONDS = 300.0
logger = logging.getLogger(__name__)
_RUNNER = r"""
const fs = require('fs');
const readline = require('readline');
const { chromium } = require('playwright');
const locator = (p,s) => {
  let l;
  if (s.role) l=p.getByRole(s.role, s.name === undefined ? {} : {name:s.name, exact:!!s.exact});
  else if (s.label) l=p.getByLabel(s.label, {exact:!!s.exact});
  else if (s.text) l=p.getByText(s.text, {exact:!!s.exact});
  else if (s.testid) l=p.getByTestId(s.testid);
  else l=p.locator(s.css || s.selector);
  return s.nth === undefined ? l : l.nth(s.nth);
};
let context=null, page=null;
const closeContext = async () => {
 if(context) await context.close().catch(()=>{});
 context=null; page=null;
};
const ensureContext = async cfg => {
 if(context && cfg.video_dir) await closeContext();
 if(!context) {
  const launch = {headless:true, viewport:cfg.viewport};
  if (cfg.executable_path) launch.executablePath = cfg.executable_path;
  if (cfg.video_dir) launch.recordVideo = {dir:cfg.video_dir, size:cfg.video_size};
  context = await chromium.launchPersistentContext(cfg.profile_dir, launch);
  const pages=context.pages(); page=pages[0] || await context.newPage();
  context.on('close',()=>{context=null;page=null;});
 }
 if(!page || page.isClosed()) {const pages=context.pages();page=pages[0] || await context.newPage();}
 await page.setViewportSize(cfg.viewport);
 return page;
};
const run = async cfg => {
 const out = {ok:false, session_id:cfg.session_id, steps:[], screenshots:[]};
 let video;
 try {
  await ensureContext(cfg);
  video=page.video(); const started=Date.now();
  page.setDefaultTimeout(cfg.timeout_ms);
  if (cfg.start_url) await page.goto(cfg.start_url, {waitUntil:'domcontentloaded'});
  for (let i=0;i<cfg.steps.length;i++) {
   if(cfg.deadline_ms && Date.now()-started > cfg.deadline_ms) throw Error('recording duration limit reached');
   const s=cfg.steps[i], op=s.op; let value=null;
   try {
    const l = ['click','fill','press','select','check','uncheck','assert','read'].includes(op) ? locator(page,s) : null;
    if(op==='goto') await page.goto(s.url,{waitUntil:s.wait_until||'domcontentloaded'});
    else if(op==='click') await l.click();
    else if(op==='fill') await l.fill(String(s.value??''));
    else if(op==='press') await l.press(s.key);
    else if(op==='select') value=await l.selectOption(s.value);
    else if(op==='check') await l.check();
    else if(op==='uncheck') await l.uncheck();
    else if(op==='wait') s.ms ? await page.waitForTimeout(s.ms) : await page.waitForLoadState(s.state||'domcontentloaded');
    else if(op==='assert') { const kind=s.kind||'visible'; if(kind==='visible') await l.waitFor({state:'visible'}); else if(kind==='hidden') await l.waitFor({state:'hidden'}); else if(kind==='text'){const got=await l.textContent();if(!String(got||'').includes(String(s.value)))throw Error(`expected text ${s.value}`); } else if(kind==='url'){if(!page.url().includes(String(s.value)))throw Error(`expected URL ${s.value}`);} else throw Error('unsupported assertion'); }
    else if(op==='read') { if(s.attribute) value=await l.getAttribute(s.attribute); else if(s.kind==='value') value=await l.inputValue(); else if(s.kind==='count') value=await l.count(); else value=(await l.innerText()).slice(0,8000); }
    else if(op==='screenshot') { const b=await page.screenshot({type:'jpeg',quality:cfg.image_quality,fullPage:!!s.full_page}); out.screenshots.push({step:i,data:b.toString('base64'),mime_type:'image/jpeg'}); }
    else throw Error(`unsupported operation: ${op}`);
    out.steps.push({i,op,ok:true,...(value===null?{}:{value})});
   } catch(e) { out.steps.push({i,op,ok:false,error:String(e.message||e).slice(0,500)}); if(cfg.screenshot_on_failure){const b=await page.screenshot({type:'jpeg',quality:cfg.image_quality});out.screenshots.push({step:i,data:b.toString('base64'),mime_type:'image/jpeg',failure:true});} throw e; }
  }
  if(cfg.final_screenshot){const b=await page.screenshot({type:'jpeg',quality:cfg.image_quality});out.screenshots.push({step:'final',data:b.toString('base64'),mime_type:'image/jpeg'});}
  out.ok=true; out.url=page.url(); out.title=await page.title();
 } catch(e) {out.error=String(e.message||e).slice(0,1000);}
 finally {
  if(cfg.video_dir) await closeContext();
  if(video && cfg.video_dir){try{out.video_path=await video.path();}catch{}}
 }
 return out;
};
const rl=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
rl.on('line', line => {
 rl.pause();
 (async()=>run(JSON.parse(line)))()
  .then(out=>process.stdout.write(JSON.stringify(out)+'\n'))
  .catch(e=>process.stdout.write(JSON.stringify({ok:false,error:String(e)})+'\n'))
  .finally(()=>rl.resume());
});
rl.on('close', async()=>{await closeContext(); process.exit(0);});
for(const signal of ['SIGTERM','SIGINT']) process.on(signal,async()=>{await closeContext();process.exit(0);});
"""


def _loop_lock(current: asyncio.Lock | None) -> asyncio.Lock:
    """Return a lock belonging to the active event loop (tests create many loops)."""
    if current is None or getattr(current, "_loop", None) not in (None, asyncio.get_running_loop()):
        return asyncio.Lock()
    return current


async def _node_path() -> str:
    global _NODE_PATH, _NODE_PATH_LOCK
    if _NODE_PATH is not None:
        return _NODE_PATH
    _NODE_PATH_LOCK = _loop_lock(_NODE_PATH_LOCK)
    async with _NODE_PATH_LOCK:
        if _NODE_PATH is None:
            npm = await asyncio.create_subprocess_exec(
                "npm", "root", "-g", stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            npm_out, npm_err = await npm.communicate()
            if npm.returncode:
                raise RuntimeError(
                    f"Unable to locate global Node modules: {npm_err.decode(errors='replace')[:300]}"
                )
            _NODE_PATH = os.pathsep.join(filter(None, [
                "/opt/portacode-playwright/node_modules", npm_out.decode().strip(),
            ]))
    return _NODE_PATH


class _BrowserWorker:
    def __init__(self, *, user: str, session_id: str, argv: list[str], env: dict[str, str]):
        self.user = user
        self.session_id = session_id
        self.argv = argv
        self.env = env
        self.process: asyncio.subprocess.Process | None = None
        self.stderr_task: asyncio.Task | None = None
        self.stderr_tail = bytearray()
        self.lock = asyncio.Lock()
        self.idle_task: asyncio.Task | None = None
        self.last_used = time.monotonic()

    async def _start(self) -> None:
        if self.process and self.process.returncode is None:
            return
        self.process = await asyncio.create_subprocess_exec(
            *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self.env,
        )
        self.stderr_task = asyncio.create_task(self._drain_stderr(self.process))

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        if not process.stderr:
            return
        try:
            while chunk := await process.stderr.read(4096):
                self.stderr_tail.extend(chunk)
                del self.stderr_tail[:-65536]
        except (asyncio.CancelledError, ValueError):
            pass

    def _schedule_idle_close(self) -> None:
        if self.idle_task:
            self.idle_task.cancel()
        self.last_used = time.monotonic()
        self.idle_task = asyncio.create_task(self._close_when_idle())

    async def _close_when_idle(self) -> None:
        try:
            await asyncio.sleep(_WORKER_IDLE_SECONDS)
            async with self.lock:
                if time.monotonic() - self.last_used >= _WORKER_IDLE_SECONDS:
                    await self._stop_locked()
                    _WORKERS.pop((self.user, self.session_id), None)
        except asyncio.CancelledError:
            pass

    async def run(self, config: dict[str, Any], timeout: float) -> dict[str, Any]:
        async with self.lock:
            await self._start()
            assert self.process and self.process.stdin and self.process.stdout
            try:
                self.process.stdin.write((json.dumps(config) + "\n").encode())
                await self.process.stdin.drain()
                line = await asyncio.wait_for(self.process.stdout.readline(), timeout=timeout)
                if not line:
                    detail = bytes(self.stderr_tail).decode(errors="replace")[-500:]
                    await self._stop_locked()
                    raise RuntimeError(
                        f"Playwright worker exited unexpectedly: {detail}"
                    )
                result = json.loads(line)
            except (asyncio.TimeoutError, BrokenPipeError, json.JSONDecodeError) as exc:
                await self._stop_locked()
                if isinstance(exc, asyncio.TimeoutError):
                    raise RuntimeError("Browser run exceeded its bounded execution time") from exc
                raise RuntimeError(f"Playwright worker failed: {exc}") from exc
            self._schedule_idle_close()
            return result

    async def _stop_locked(self) -> None:
        process, self.process = self.process, None
        stderr_task, self.stderr_task = self.stderr_task, None
        if not process or process.returncode is not None:
            if stderr_task:
                await stderr_task
            return
        if process.stdin:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        if stderr_task:
            await stderr_task


async def _worker_for(*, user: str, session_id: str, argv: list[str], env: dict[str, str]) -> _BrowserWorker:
    global _WORKERS_LOCK
    _WORKERS_LOCK = _loop_lock(_WORKERS_LOCK)
    async with _WORKERS_LOCK:
        key = (user, session_id)
        worker = _WORKERS.get(key)
        if worker is None or worker.process and worker.process.returncode is not None:
            worker = _BrowserWorker(user=user, session_id=session_id, argv=argv, env=env)
            _WORKERS[key] = worker
        return worker


class BrowserRunHandler(AsyncHandler):
    @property
    def command_name(self) -> str:
        return "browser_run"

    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        session_id = str(message.get("session_id") or "default")
        if not _SAFE_ID.fullmatch(session_id):
            raise ValueError("session_id must contain only letters, numbers, dot, dash, or underscore")
        steps = message.get("steps") or []
        if not isinstance(steps, list) or len(steps) > 30:
            raise ValueError("steps must be an array containing at most 30 actions")
        encoded = json.dumps(steps)
        if len(encoded) > 64_000:
            raise ValueError("browser action payload is too large")
        if sum(1 for step in steps if isinstance(step, dict) and step.get("op") == "screenshot") > 4:
            raise ValueError("at most four explicit screenshots are allowed per run")
        timeout_ms = min(max(int(message.get("timeout_ms") or 10_000), 250), 30_000)
        width = min(max(int((message.get("viewport") or {}).get("width", 960)), 320), 1920)
        height = min(max(int((message.get("viewport") or {}).get("height", 540)), 240), 1080)
        user = get_default_runtime_user(message)
        root = Path(get_runtime_user_home(message)) / ".local" / "share" / "portacode" / "browser"
        profile = root / "profiles" / session_id
        video_dir = root / "recordings" / session_id if message.get("record_video") else None
        if not profile.exists():
            mkdir_with_owner(profile, user)
        if video_dir:
            if not video_dir.exists():
                mkdir_with_owner(video_dir, user)
        executable = "/usr/bin/chromium-browser" if Path("/usr/bin/chromium-browser").exists() else None
        config = {
            "session_id": session_id, "profile_dir": str(profile), "video_dir": str(video_dir) if video_dir else None,
            "video_size": {"width": min(width, 854), "height": min(height, 480)},
            "viewport": {"width": width, "height": height}, "executable_path": executable,
            "start_url": message.get("start_url"), "steps": steps, "timeout_ms": timeout_ms,
            "deadline_ms": 45_000 if video_dir else None,
            "image_quality": min(max(int(message.get("image_quality") or 65), 30), 85),
            "final_screenshot": bool(message.get("final_screenshot", True)),
            "screenshot_on_failure": bool(message.get("screenshot_on_failure", True)),
        }
        env = dict(os.environ)
        env["NODE_PATH"] = await _node_path()
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/ms-playwright")
        argv = wrap_argv_for_user(["node", "-e", _RUNNER], user, login=False)
        worker = await _worker_for(
            user=user, session_id=session_id, argv=argv, env=env,
        )
        run_limit = min(75 if video_dir else 300, 20 + len(steps) * timeout_ms / 1000)
        result = await worker.run(config, timeout=run_limit)
        result.update({"event": "browser_run_response", "success": True})
        return result
