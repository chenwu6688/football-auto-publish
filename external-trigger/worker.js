/**
 * gh-batch-trigger
 * 用 Cloudflare Cron Triggers 在准点调用 GitHub workflow_dispatch，
 * 绕开 GitHub 免费调度器在 UTC 00:00 全球零点的拥堵，保证三班准点发布。
 *
 * 触发映射（Cloudflare cron 仅支持 UTC，已换算为 CST）：
 *   7 0 * * *   -> morning   (CST 08:07)
 *   7 4 * * *   -> noon      (CST 12:07)
 *   37 9 * * *  -> evening   (CST 17:37)
 *
 * 依赖 secret：GH_TOKEN = GitHub PAT（Actions: write）
 * 依赖 KV：LOG = 观测用，记录每次 scheduled 是否被触发（沙箱可读，用于诊断）
 *
 * 注意：GitHub REST API 强制要求 User-Agent 头，否则一律 403。
 *       Worker 的 fetch() 默认不带该头，必须显式加上（curl 会自动带，故沙箱测试能过）。
 */

const REPO = 'chenwu6688/football-auto-publish';
const WORKFLOW = 'batch.yml';
const REF = 'main';

// 写入观测日志到 KV（沙箱可通过 CF API 读取，绕开 workers.dev 网络限制）
async function mark(env, obj) {
  if (!env.LOG) return;
  try {
    const prev = await env.LOG.get('last_scheduled');
    let arr = [];
    if (prev) { try { arr = JSON.parse(prev); if (!Array.isArray(arr)) arr = [arr]; } catch { arr = []; } }
    arr.push({ t: Date.now(), ...obj });
    if (arr.length > 10) arr = arr.slice(-10);
    await env.LOG.put('last_scheduled', JSON.stringify(arr));
  } catch (e) {
    // 观测失败不影响主流程
  }
}

async function dispatch(batch, env) {
  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${env.GH_TOKEN}`,
      'Accept': 'application/vnd.github+json',
      'Content-Type': 'application/json',
      'X-GitHub-Api-Version': '2022-11-28',
      // GitHub API 强制要求 User-Agent 头，否则返回 403（fetch 默认不带，curl 会带）
      'User-Agent': 'cloudflare-worker-gh-batch-trigger',
    },
    body: JSON.stringify({ ref: REF, inputs: { batch } }),
  });
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`dispatch ${batch} failed: ${res.status} ${text}`);
  }
  return new Response(`dispatched ${batch}`, { status: 200 });
}

function batchFromCron(cron) {
  let batch = 'morning';
  if (cron.startsWith('7 4'))       batch = 'noon';
  else if (cron.startsWith('37 9')) batch = 'evening';
  return batch;
}

export default {
  async scheduled(event, env, ctx) {
    const batch = batchFromCron(event.cron);
    // 第一件事就记录：证明 CF 确实调用了 scheduled（无论后面 dispatch 成败）
    await mark(env, { via: 'scheduled', cron: event.cron, batch, stage: 'start' });
    try {
      const r = await dispatch(batch, env);
      await mark(env, { via: 'scheduled', cron: event.cron, batch, stage: 'ok' });
      return r;
    } catch (e) {
      await mark(env, { via: 'scheduled', cron: event.cron, batch, stage: 'error', error: String(e) });
      throw e;
    }
  },
};
