# market-monitor 微信小程序（web-view 嵌 H5）

最快上线形态：小程序本身只用一个 `web-view` 页面，把现有工作台 H5 直接嵌进来，
**不需要重写前端**。后端沿用本仓库的 Flask（`app.py`，端口 8765），只要把它部署到公网 HTTPS 域名即可。

## 你要准备的两样东西

1. **一个 HTTPS 公网域名**（需 ICP 备案）。可用云服务器（ECS / 轻量应用服务器）+ 反向代理（nginx / Caddy）+ Let's Encrypt 证书。
2. **一个小程序 AppID**（在 https://mp.weixin.qq.com 注册小程序后获得）。

## 上线步骤

1. 打开 `pages/index/index.js`，把 `https://YOUR_DOMAIN_HERE/` 改成你的真实域名（结尾带 `/`）。
2. **小程序后台 → 开发 → 开发设置 → 业务域名**：把该域名加白。
   - 小程序 `web-view` 只能加载业务域名下的 H5；未加白会白屏。
   - 按页面提示下载校验文件，放到该域名服务器根目录（如 `/.well-known/...`），验证通过才生效。
3. **部署 Flask 到该域名**：用 nginx/Caddy 反代本机 `127.0.0.1:8765` 到 `https://你的域名`，并放行公网 443。
   - 工作台访问密钥仍走 `WORKBENCH_KEY` / `data/access_key.json`，公网访问请确保密钥强度（不要裸奔）。
   - 仅读行情 + 模拟盘，不接实盘下单，公网暴露风险可控。
4. **微信开发者工具**打开本目录，`project.config.json` 里的 `appid` 改成你自己的（当前 `touristappid` 只是游客模式，不能真机预览/发布）。
   - 本地调试可在「详情 → 本地设置」勾选「不校验合法域名」临时绕过业务域名校验。
5. 真机预览 → 上传代码 → 提交审核 → 发布。

## 目录结构

```
app.json / app.js / app.wxss        小程序外壳配置
project.config.json                 开发者工具项目配置（AppID 占位）
sitemap.json
pages/index/                        唯一页面：web-view 加载 H5 工作台
```

> 进阶（可选）：若要在小程序内调用微信能力（分享、订阅消息等），需引入微信 JSSDK 并在 H5 内注入，
> 不在本最小骨架范围内。
