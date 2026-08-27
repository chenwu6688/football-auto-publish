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
 * 依赖 secret：GH_TOKEN = GitHub Fine-grained PAT
 *   （仅授权仓库 chenwu6688/football-auto-publish 的 Actions: Read and write）
 */

const REPO = 'chenwu6688/football-auto-publish';
const WORKFLOW = 'batch.yml';
const REF = 'main';

export default {
  async scheduled(event, env, ctx) {
    const cron = event.cron;
    let batch = 'morning';
    if (cron.startsWith('7 4'))       batch = 'noon';
    else if (cron.startsWith('37 9')) batch = 'evening';

    console.log(`[gh-batch-trigger] cron=${cron} -> batch=${batch}`);

    const url = `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${env.GH_TOKEN}`,
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
        'X-GitHub-Api-Version': '2022-11-28',
      },
      body: JSON.stringify({ ref: REF, inputs: { batch } }),
    });

    const text = await res.text();
    if (!res.ok) {
      console.error(`[gh-batch-trigger] FAILED ${res.status}: ${text}`);
      throw new Error(`dispatch ${batch} failed: ${res.status} ${text}`);
    }
    console.log(`[gh-batch-trigger] OK dispatched ${batch} (${res.status})`);
    return new Response(`dispatched ${batch}`, { status: 200 });
  },
};
