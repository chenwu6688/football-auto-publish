/**
 * 首页 Feed — 展示今日文章列表
 */
const api = require('../../utils/api');

Page({
  data: {
    // 页面状态: loading | ready | error | empty
    status: 'loading',
    errorMsg: '',

    // 文章数据
    today: null,
    batches: [],
    articles: [],

    // 批次 Tab
    activeTab: 0,
    tabNames: ['全部'],
    // 当前选中批次的展示信息（图标+名称+文章数）
    currentBatchInfo: { icon: '', name: '', count: 0 },
  },

  onLoad() {
    this.loadData();
  },

  onPullDownRefresh() {
    this.loadData(true);
  },

  onShareAppMessage() {
    const a = this.data.articles[0] || {};
    return {
      title: '球评人老六 — 每天6篇足球辣评',
      path: '/pages/index/index',
      imageUrl: a.cover_image || '',
    };
  },

  onShareTimeline() {
    const a = this.data.articles[0] || {};
    return {
      title: '球评人老六 — 每天6篇足球辣评',
      imageUrl: a.cover_image || '',
    };
  },

  getBatchIcon(batchName) {
    if (batchName === '晨读') return '🌅';
    if (batchName === '午间') return '☀️';
    return '🌙';
  },

  // 计算当前选中批次的展示信息
  computeBatchInfo(batches, tabNames, activeTab) {
    if (activeTab === 0 || !batches || batches.length === 0) {
      return { icon: '', name: '', count: 0 };
    }
    const batch = batches[activeTab - 1] || {};
    const articles = batch.articles || [];
    return {
      icon: this.getBatchIcon(batch.batch_name),
      name: batch.batch_name || '',
      count: articles.length,
    };
  },

  loadData(fromRefresh) {
    if (!fromRefresh) {
      this.setData({ status: 'loading' });
    }

    api.fetchToday()
      .then((data) => {
        if (!data.articles || data.articles.length === 0) {
          this.setData({ status: 'empty', debugInfo: '服务器返回0篇文章' });
          return;
        }

        // 构建批次 tab（跳过空 batch_name 的批次）
        const tabNames = ['全部'];
        const validBatches = [];
        for (const b of data.batches || []) {
          if (b.batch_name && b.articles && b.articles.length > 0) {
            tabNames.push(b.batch_name);
            validBatches.push(b);
          }
        }

        // 按批次排序文章
        const batchOrder = { '晨读': 0, '午间': 1, '晚间': 2 };
        const articles = (data.articles || []).sort((a, b) => {
          const oa = batchOrder[a.batch_name] !== undefined ? batchOrder[a.batch_name] : 99;
          const ob = batchOrder[b.batch_name] !== undefined ? batchOrder[b.batch_name] : 99;
          return oa - ob;
        });

        this.setData({
          status: 'ready',
          today: data,
          batches: validBatches,
          articles,
          tabNames,
          activeTab: 0,
          currentBatchInfo: { icon: '', name: '', count: 0 },
          debugInfo: `已加载 ${data.articles.length} 篇文章`,
        });
      })
      .catch((err) => {
        this.setData({ status: 'error', errorMsg: err.message });
      })
      .finally(() => {
        if (fromRefresh) wx.stopPullDownRefresh();
      });
  },

  switchTab(e) {
    const idx = e.currentTarget.dataset.index;
    if (idx === this.data.activeTab) return;

    const batchName = this.data.tabNames[idx];
    let filtered;

    if (idx === 0) {
      // "全部" — 展示所有文章
      const batchOrder = { '晨读': 0, '午间': 1, '晚间': 2 };
      filtered = [...this.data.today.articles].sort((a, b) => {
        const oa = batchOrder[a.batch_name] !== undefined ? batchOrder[a.batch_name] : 99;
        const ob = batchOrder[b.batch_name] !== undefined ? batchOrder[b.batch_name] : 99;
        return oa - ob;
      });
    } else {
      filtered = this.data.today.articles.filter(
        (a) => a.batch_name === batchName
      );
    }

    const batchInfo = this.computeBatchInfo(this.data.batches, this.data.tabNames, idx);
    this.setData({ activeTab: idx, articles: filtered, currentBatchInfo: batchInfo });
  },

  onCoverError(e) {
    // 封面图加载失败 → 隐藏封面区域
    const idx = e.currentTarget.dataset.index;
    const key = `articles[${idx}].cover_image`;
    this.setData({ [key]: '' });
  },

  openArticle(e) {
    const id = e.currentTarget.dataset.id;
    wx.navigateTo({ url: `/pages/article/article?id=${id}` });
  },
});
