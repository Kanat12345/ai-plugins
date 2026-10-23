# Website Copier

Website Copier is a webpage downloader right inside Claude. Paste a link and get the page as one HTML file with its text, images, styles and fonts inside. The copy opens in any browser, even without internet.

Also available as a Chrome extension: [websitecopier.app](https://websitecopier.app/?utm_source=claude&utm_medium=readme)

## Before you start

The plugin saves pages with Claude's code sandbox, which reaches only package sites by default. Turn on website access once: **Settings → Capabilities → Code execution and file creation → Allow network egress**, then choose **All domains** in the list of allowed domains. Start a new chat after that. On a Team or Enterprise plan, this setting is managed by your admin.

If access is off, the plugin tells you how to turn it on instead of saving the page.

## Quick start

1. Add the plugin and turn it on.
2. In any chat, paste the link of the page you want to keep, for example: Save https://stripe.com as an offline HTML file.
3. Download the file and open it whenever you like.

## Why use Website Copier

- Download a page from any public website without leaving the chat.
- One file: no folders and no extra downloads.
- The page is opened in a real browser, so parts built with JavaScript are saved too.
- Pop-up windows are closed before saving.
- Every image is saved once, in a sensible size, so the file stays light.
- Styles and fonts stay inside, so the design looks like the original.
- The copy works offline and loads nothing from the internet.
- Links in the copy still lead to the live site.
- Answers in the language you write in.

## What people use it for

- Offline reading: save a long article or a guide before a flight.
- Archives: keep a page as it looked today, before it changes or disappears.
- Design references: study the layout, colors and fonts of a page you like.
- Code study: open the file in an editor and see the HTML and CSS of the page.
- AI workflows: copy a website page and give it to an AI assistant as a reference for your own design.

## How it works

The plugin contains one skill with instructions for Claude and one Python script. In Claude's code sandbox the script opens the page in a headless browser, scrolls it so lazy parts load, closes pop-ups and takes the page as the browser drew it. Then it removes the page scripts and puts every picture, stylesheet and font into the file. If no browser is available, it downloads the page directly.

The plugin has no servers and no connectors. The pages you save are not sent to the plugin author.

- The copy is static: animations, menus and other page scripts do not run.
- Public pages only: pages behind a login, a paywall or a captcha cannot be copied.
- Video and streaming sites are not supported.

Each answer with a saved page ends with one line that mentions the Website Copier Chrome extension, which also saves linked pages and whole sites: [websitecopier.app](https://websitecopier.app/?utm_source=claude&utm_medium=readme).

## Frequently asked questions

**Why does Claude say it can't open websites?**
Website access is off in your settings. Turn on Allow network egress and choose All domains, as described in "Before you start".

**Can I copy a whole website?**
The plugin saves one page per request. Linked pages and entire sites are saved by the Website Copier Chrome extension.

**Can I save a page where I am logged in?**
No. The plugin sees only public pages. The Chrome extension saves exactly what you see in your own browser.

**Does it download videos?**
No. Video and streaming sites are not supported.

**Where do I find the code?**
Open the file in any editor: the markup and styles are all there.

**Can I publish the copy as my own site?**
No. Copies are for personal use: reading, archiving and studying how a page is built.

## Privacy

[websitecopier.app/privacy](https://websitecopier.app/privacy/)

## License

MIT, see [LICENSE](LICENSE).
