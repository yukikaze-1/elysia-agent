import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { marked } from 'marked'
import { markedHighlight } from 'marked-highlight'
import hljs from 'highlight.js'
import App from './App.vue'
import './styles/variables.css'

marked.use(markedHighlight({
  langPrefix: 'hljs language-',
  highlight(code: string, lang: string) {
    if (lang && hljs.getLanguage(lang)) {
      return hljs.highlight(code, { language: lang }).value
    }
    return hljs.highlightAuto(code).value
  },
}))

const app = createApp(App)
app.use(createPinia())
app.mount('#app')
