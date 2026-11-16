<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const shifts = ref([])
const fromId = ref(1)
const toId = ref(2)
const segSize = ref('')
const plan = ref(null)
const msg = ref('')
const ok = ref(false)

const active = computed(() => !!plan.value && plan.value.status === '进行中')
const nextSeq = computed(() => {
  if (!plan.value || plan.value.status !== '进行中') return null
  const s = (plan.value.segments || []).find((x) => x.status === '待处理')
  return s ? s.seq : null
})

// 并集校验视图：终态后展示物化报告；进行中展示实时预览（发起切分校验已物化）
const checkView = computed(() => {
  if (!plan.value) return null
  const r = plan.value.reconcile
  if (r && r.stage === '终态定案') {
    return {
      source: `物化报告 #${r.id} · ${r.stage} · ${r.created_at}`,
      快照键数: r.snapshot_count,
      并集键数: r.union_count,
      缺失键: r.detail.缺失键,
      多出键: r.detail.多出键,
      段间相交键: r.detail.段间相交键,
      段间互斥: r.disjoint,
      并集完整: r.union_ok,
      每段成功: r.all_segments_ok,
      计划成功: r.plan_ok,
    }
  }
  const u = plan.value.union_check
  return {
    source: r
      ? `实时预览 · 发起切分校验已物化为报告 #${r.id}（并集${r.union_ok ? '完整' : '缺损'}），终态后定案落档`
      : '实时预览',
    ...u,
  }
})

function segTagClass(status) {
  if (status === '已完成') return 'on'
  if (status === '失败') return 'bad'
  return ''
}

async function refresh() {
  shifts.value = await getJson('/api/shifts')
  const plans = await getJson('/api/handovers')
  const cur = plans.find((p) => p.status === '进行中') || plans[0] || null
  plan.value = cur ? await getJson(`/api/handovers/${cur.id}`) : null
}

onMounted(async () => {
  try {
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
  }
})

async function start() {
  msg.value = ''
  ok.value = false
  try {
    const body = { from_shift_id: Number(fromId.value), to_shift_id: Number(toId.value) }
    if (segSize.value !== '' && segSize.value !== null) body.segment_size = Number(segSize.value)
    plan.value = await postJson('/api/handovers/start', body)
    ok.value = true
    msg.value = `已开交班计划 #${plan.value.id}：快照 ${plan.value.batch_count} 键切成 ${plan.value.segment_count} 段（每段至多 ${plan.value.segment_size} 键）`
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
  }
}

async function completeSeg(seq) {
  msg.value = ''
  ok.value = false
  try {
    plan.value = await postJson(`/api/handovers/${plan.value.id}/segments/${seq}/complete`)
    reportResult(`第 ${seq} 段`)
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
  }
}

async function completeAll() {
  msg.value = ''
  ok.value = false
  try {
    plan.value = await postJson(`/api/handovers/${plan.value.id}/complete`)
    reportResult('全部剩余段')
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
  }
}

function reportResult(label) {
  if (plan.value.status === '已完成') {
    ok.value = true
    msg.value = `${label}已办理：计划 #${plan.value.id} 完成，全部批次已过户，并集校验通过`
  } else if (plan.value.status === '失败') {
    ok.value = false
    msg.value = `计划 #${plan.value.id} 失败：${plan.value.reason}（已完成段过户保留，不回滚）`
  } else {
    ok.value = true
    msg.value = `${label}已办理，计划 #${plan.value.id} 进行中：${plan.value.done_segments}/${plan.value.segment_count} 段完成`
  }
}
</script>

<template>
  <div>
    <h1>交班办理</h1>
    <p class="lead">
      发起交班可设分段上限 N（≥1）：在途快照一次性切成有序段链，按序逐段过户；
      任一段失败则后续段阻断、计划标失败，已完成段的过户不回滚。
    </p>

    <div class="panel">
      <div class="row">
        <label>交班方
          <select v-model.number="fromId">
            <option v-for="s in shifts" :key="s.id" :value="s.id">{{ s.name }} · {{ s.keeper }}</option>
          </select>
        </label>
        <label>接班方
          <select v-model.number="toId">
            <option v-for="s in shifts" :key="'t'+s.id" :value="s.id">{{ s.name }} · {{ s.keeper }}</option>
          </select>
        </label>
        <label>分段上限 N（每段至多 N 键，留空=单段全量）
          <input v-model="segSize" type="number" min="1" step="1" placeholder="留空 = 单段全量" />
        </label>
        <button class="btn" :disabled="active" @click="start">发起交班</button>
        <button class="btn ghost" :disabled="!active" @click="completeAll">按序完成全部剩余段</button>
      </div>
      <p class="msg" :class="{ ok }">{{ msg }}</p>
    </div>

    <div class="panel" v-if="plan">
      <h2>
        交班计划 #{{ plan.id }} ·
        <span class="tag" :class="plan.status === '已完成' ? 'on' : plan.status === '失败' ? 'bad' : ''">{{ plan.status }}</span>
      </h2>
      <p class="lead" style="margin-bottom:12px">
        {{ plan.from_shift_name }} → {{ plan.to_shift_name }} · 快照 {{ plan.batch_count }} 键 ·
        每段上限 {{ plan.segment_size }} · 共 {{ plan.segment_count }} 段 ·
        已完成 {{ plan.done_segments }}/{{ plan.segment_count }} · 发起 {{ plan.started_at }}
        <template v-if="plan.finished_at"> · 结束 {{ plan.finished_at }}</template>
      </p>
      <p v-if="plan.reason" class="msg">{{ plan.reason }}</p>

      <h2>段链条（必须按序完成）</h2>
      <table>
        <thead>
          <tr><th>段</th><th>状态</th><th>键数</th><th>业务键</th><th>完成时间</th><th>原因</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="s in plan.segments" :key="s.seq">
            <td>第 {{ s.seq }} 段</td>
            <td><span class="tag" :class="segTagClass(s.status)">{{ s.status }}</span></td>
            <td>{{ s.batch_count }}</td>
            <td>{{ s.keys.join('、') }}</td>
            <td>{{ s.finished_at || '—' }}</td>
            <td>{{ s.reason || '—' }}</td>
            <td>
              <button v-if="s.seq === nextSeq" class="btn ghost" @click="completeSeg(s.seq)">完成本段</button>
              <span v-else>—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" v-if="plan && checkView">
      <h2>计划级并集对账</h2>
      <p class="lead" style="margin-bottom:12px">{{ checkView.source }} · 各段清单并集须等于发起快照，段间键不交</p>
      <div class="stats">
        <div class="stat"><div class="k">快照键数</div><div class="v">{{ checkView.快照键数 }}</div></div>
        <div class="stat"><div class="k">段清单并集键数</div><div class="v">{{ checkView.并集键数 }}</div></div>
        <div class="stat">
          <div class="k">缺失 / 多出 / 段间相交</div>
          <div class="v">{{ checkView.缺失键.length }} / {{ checkView.多出键.length }} / {{ checkView.段间相交键.length }}</div>
        </div>
      </div>
      <p class="tags">
        <span class="tag" :class="checkView.段间互斥 ? 'on' : 'bad'">段间互斥 {{ checkView.段间互斥 ? '✓' : '✗' }}</span>
        <span class="tag" :class="checkView.并集完整 ? 'on' : 'bad'">并集完整 {{ checkView.并集完整 ? '✓' : '✗' }}</span>
        <span class="tag" :class="checkView.每段成功 ? 'on' : 'bad'">每段成功 {{ checkView.每段成功 ? '✓' : '✗' }}</span>
        <span class="tag" :class="checkView.计划成功 ? 'on' : 'bad'">计划成功 {{ checkView.计划成功 ? '✓' : '✗' }}</span>
      </p>
      <p v-if="checkView.缺失键.length" class="msg">缺失键：{{ checkView.缺失键.join('、') }}</p>
      <p v-if="checkView.多出键.length" class="msg">多出键：{{ checkView.多出键.join('、') }}</p>
      <p v-if="checkView.段间相交键.length" class="msg">段间相交键：{{ checkView.段间相交键.join('、') }}</p>
    </div>
  </div>
</template>
