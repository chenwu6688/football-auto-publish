/**
 * 文章详情页
 */
const api = require('../../utils/api');

Page({
  data: {
    status: 'loading',
    article: null,
  },

  onLoad(options) {
    if (options.id) {
      this.loadArticle(options.id);
    } else {
      this.setData({ status: 'error', errorMsg: '缺少文章 ID' });
    }
  },

  onShareAppMessage() {
    const a = this.data.article;
    if (!a) return { title: '球评人老六' };
    return {
      title: a.title,
      path: `/pages/article/article?id=${a.id}`,
      imageUrl: a.cover_image || '',
    };
  },

  onShareTimeline() {
    const a = this.data.article;
    if (!a) return { title: '球评人老六' };
    return {
      title: a.title,
      imageUrl: a.cover_image || '',
    };
  },

  loadArticle(id) {
    this.setData({ status: 'loading' });

    api.fetchToday()
      .then((data) => {
        if (!data.articles || data.articles.length === 0) {
          this.setData({ status: 'empty' });
          return;
        }

        const article = data.articles.find((a) => a.id === id);
        if (!article) {
          this.setData({ status: 'error', errorMsg: '文章未找到' });
          return;
        }

        // 设置页面标题
        wx.setNavigationBarTitle({ title: article.title.slice(0, 20) + '...' });

        // 清理正文中的图片标签（CDN 图片在国内访问不稳定，优先保证文字可读）
        let cleanHtml = (article.html_content || '')
          .replace(/<img[^>]*>/g, '')       // 移除 <img> 标签
          .replace(/<figure[^>]*>[\s\S]*?<\/figure>/g, '')  // 移除 figure
          .trim();
        // 清理多余空行
        cleanHtml = cleanHtml.replace(/<p>\s*<\/p>/g, '');
        article.html_content = cleanHtml || '<p>内容加载中...</p>';

        this.setData({ status: 'ready', article });
      })
      .catch((err) => {
        this.setData({ status: 'error', errorMsg: err.message });
      });
  },

  onCoverError() {
    // 封面图加载失败 → 隐藏
    const a = this.data.article;
    if (a) {
      a.cover_image = '';
      this.setData({ article: a });
    }
  },

  showReward() {
    wx.showModal({
      title: '☕ 请老六喝咖啡',
      content: '感谢支持！你可以通过微信赞赏码支持老六的每日创作。',
      confirmText: '好的',
      showCancel: false,
    });
  },
});
