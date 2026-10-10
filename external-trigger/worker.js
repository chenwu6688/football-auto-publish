/**
 * gh-batch-trigger —— 唯一主调度（准点）
 *
 * 用 Cloudflare Cron Triggers 在准点调用 GitHub workflow_dispatch，
 * 绕开 GitHub 免费调度器在 UTC 00:00 全球零点的拥堵，保证三班准点发布。
 *
 * 角色：本 Worker 是项目的【唯一主调度】。GitHub Actions batch.yml 的原生
 * cron 仅作为「单一兜底」（比本触发晚 30 分钟），靠 batch.yml 的幂等保护
 * 跳过已被本触发完成的批次，不会重复发文。详见 external-trigger/README.md。
 *
 * 触发映射（Cloudflare cron 仅支持 UTC，已换算为 CST）：
 *   0 0 * * *   -> morning   (CST 08:00)
 *   0 4 * * *   -> noon      (CST 12:00)
 *   30 9 * * *  -> evening   (CST 17:30)
 *
 * 依赖 secret：GH_TOKEN = GitHub Fine-grained PAT（仅本仓库 Actions: write，
 *             务必设 1 年有效期，到期前在 Cloudflare 侧更新，否则主调度静默失效）
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
  const parts = cron.trim().split(/\s+/);
  const hour = parseInt(parts[1], 10);
  const minute = parseInt(parts[0], 10);
  // 主调度 cron（UTC）：0 0=晨读, 0 4=午间, 30 9=晚间（见 wrangler.toml）
  if (hour === 0) return 'morning';
  if (hour === 4) return 'noon';
  if ((hour === 9 && minute >= 30) || hour === 10) return 'evening';
  return 'morning';
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
