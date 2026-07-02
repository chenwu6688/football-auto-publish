App({
  globalData: {
    CDN_BASE: 'https://cdn.jsdelivr.net/gh/chenwu6688/football-auto-publish@main',
    // 用于缓存的数据
    todayData: null,
    historyData: null,
  },
  onLaunch() {
    // 检查更新
    const updateManager = wx.getUpdateManager();
    updateManager.onUpdateReady(() => {
      wx.showModal({
        title: '更新提示',
        content: '新版本已准备好，是否重启应用？',
        success: (res) => {
          if (res.confirm) updateManager.applyUpdate();
        }
      });
    });
  },
});
