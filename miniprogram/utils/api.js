/**
 * API 工具 — 从 jsDelivr CDN 读取静态数据
 */
const CDN_BASE = 'https://cdn.jsdelivr.net/gh/chenwu6688/football-auto-publish@main';

/**
 * 获取今日文章数据（带缓存破坏参数）
 */
function fetchToday() {
  return new Promise((resolve, reject) => {
    const ts = Date.now();
    wx.request({
      url: `${CDN_BASE}/static_data/v3_today.json?_=${ts}`,
      method: 'GET',
      timeout: 15000,
      success: (res) => {
        if (res.statusCode === 200 && res.data) {
          const data = res.data;
          // 验证数据完整性
          if (!data.articles || !Array.isArray(data.articles)) {
            reject(new Error('数据格式异常：articles 不是数组'));
            return;
          }
          console.log(`[mini] 加载成功: ${data.articles.length} 篇文章, 日期=${data.date}`);
          resolve(data);
        } else {
          reject(new Error(`数据加载失败 (HTTP ${res.statusCode})`));
        }
      },
      fail: (err) => {
        console.error('[mini] 网络请求失败:', err);
        reject(new Error('网络请求失败，请检查网络后重试'));
      }
    });
  });
}

/**
 * 获取历史日期列表
 */
function fetchHistory() {
  return new Promise((resolve, reject) => {
    wx.request({
      url: `${CDN_BASE}/static_data/history.json`,
      method: 'GET',
      timeout: 10000,
      success: (res) => {
        if (res.statusCode === 200 && res.data) {
          resolve(res.data);
        } else {
          reject(new Error('历史数据加载失败'));
        }
      },
      fail: () => {
        reject(new Error('网络请求失败'));
      }
    });
  });
}

module.exports = {
  fetchToday,
  fetchHistory,
  CDN_BASE,
};
