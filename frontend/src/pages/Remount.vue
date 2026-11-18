<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const oldKey = ref('WB-OLD')
const newKey = ref('WB-NEW')
const newTitle = ref('')
const busy = ref(false)
const err = ref('')
const result = ref(null)
const edges = ref([])
const proof = ref(null)
const proofId = ref(null)
const listCert = ref(null)

async function loadEdges() {
  edges.value = await api.remounts()
  listCert.value = await api.remountListRetirement()
}

async function submit() {
  busy.value = true
  err.value = ''
  result.value = null
  try {
    const res = await api.remount(oldKey.value.trim(), newKey.value.trim(), newTitle.value.trim())
    result.value = res.edge
    await loadEdges()
  } catch (e) {
    // FastAPI 409 错误体：{ detail: { message, ... } } 或字符串
    try {
      const body = JSON.parse(e.message)
      err.value = body.detail?.message || body.detail || String(e.message)
    } catch (_) {
      err.value = String(e.message || e)
    }
  } finally {
    busy.value = false
  }
}

async function showProof(id) {
  proofId.value = id
  proof.value = await api.remountProof(id)
}

onMounted(loadEdges)
</script>

<template>
  <div class="page-card">
    <h1>运单键重挂</h1>
    <p class="lead">
      同事务将旧键正式有效点整段挂到新键并重算双侧投影；新键已有点即冲突回滚。
      完成后旧键退休（410，不再提供有效轴），新键承接全序，迁移边不可变可审计。
    </p>

    <form class="remount-form" @submit.prevent="submit">
      <label>旧键
        <input v-model="oldKey" placeholder="WB-OLD" required />
      </label>
      <label>新键
        <input v-model="newKey" placeholder="WB-NEW" required />
      </label>
      <label>新键标题（可选）
        <input v-model="newTitle" placeholder="留空则自动派生" />
      </label>
      <button class="btn" type="submit" :disabled="busy">
        {{ busy ? '迁移中…' : '执行重挂（同事务）' }}
      </button>
    </form>

    <p v-if="err" class="muted remount-err">迁移被拒绝并已回滚：{{ err }}</p>

    <div v-if="result" class="remount-result">
      <h2>重挂证明 #{{ result.id }}</h2>
      <dl class="kv">
        <dt>迁移边</dt><dd>{{ result.edge_uid }}</dd>
        <dt>旧键 → 新键</dt><dd>{{ result.old_key }} → {{ result.new_key }}</dd>
        <dt>迁入点数</dt><dd>{{ result.point_count }}（共移动 {{ result.hops_moved_total }} 行）</dd>
        <dt>旧投影 → 新投影</dt><dd>{{ result.old_proj }} → {{ result.new_proj }}</dd>
        <dt>完成时刻</dt><dd>{{ result.completed_at }}</dd>
        <dt>旧轴</dt><dd class="mono">{{ result.old_axis.join(' → ') }}</dd>
        <dt>新轴</dt><dd class="mono">{{ result.new_axis.join(' → ') }}</dd>
      </dl>
    </div>

    <h2 style="margin-top:28px">列表退役核证</h2>
    <p class="lead">
      只读核验：每条迁移边的源键已从对外有效运单列表 / 检索入口消失，目标键承接。
    </p>
    <p v-if="listCert" :class="listCert.ok ? 'muted' : 'remount-err'">
      {{ listCert.ok ? '核证通过：源键均已退役，列表与检索不再列出。' : '核证失败：仍有源键出现在有效列表 / 检索中。' }}
    </p>
    <table class="table" v-if="listCert">
      <thead>
        <tr><th>边</th><th>源键</th><th>目标键</th><th>退役标记</th><th>在有效列表</th><th>在检索</th><th>目标在列表</th><th>核证</th></tr>
      </thead>
      <tbody>
        <tr v-for="c in listCert.edges" :key="c.edge_id">
          <td>{{ c.edge_id }}</td>
          <td>{{ c.old_key }}</td>
          <td>{{ c.new_key }}</td>
          <td>{{ c.old_key_retired ? '是' : '否' }}</td>
          <td>{{ c.old_key_in_active_list ? '是（违规）' : '否' }}</td>
          <td>{{ c.old_key_in_search ? '是（违规）' : '否' }}</td>
          <td>{{ c.new_key_in_active_list ? '是' : '否（违规）' }}</td>
          <td>{{ c.ok ? '通过' : '失败' }}</td>
        </tr>
        <tr v-if="!listCert.edges.length"><td colspan="8" class="muted">暂无迁移边</td></tr>
      </tbody>
    </table>

    <h2 style="margin-top:28px">迁移边审计</h2>
    <table class="table">
      <thead>
        <tr><th>id</th><th>边 uid</th><th>旧键</th><th>新键</th><th>点数</th><th>投影</th><th>时刻</th><th></th></tr>
      </thead>
      <tbody>
        <tr v-for="e in edges" :key="e.id">
          <td>{{ e.id }}</td>
          <td class="mono">{{ e.edge_uid }}</td>
          <td>{{ e.old_key }}</td>
          <td>{{ e.new_key }}</td>
          <td>{{ e.point_count }}</td>
          <td>{{ e.old_proj }} → {{ e.new_proj }}</td>
          <td>{{ e.completed_at }}</td>
          <td><button class="btn btn-mini" @click="showProof(e.id)">证明</button></td>
        </tr>
        <tr v-if="!edges.length"><td colspan="8" class="muted">暂无迁移边</td></tr>
      </tbody>
    </table>

    <div v-if="proof" class="remount-result">
      <h2>只读重挂证明 #{{ proofId }}</h2>
      <pre class="proof-pre">{{ JSON.stringify(proof, null, 2) }}</pre>
    </div>
  </div>
</template>
