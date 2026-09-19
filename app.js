async function callNemotron(action, payload = {}) {
  const response = await fetch('/api/nemotron', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ action, payload })
  });

  if (!response.ok) {
    throw new Error('Nemotron request failed');
  }

  return response.json();
}

function renderTasks(tasks) {
  const list = document.getElementById('taskList');
  list.innerHTML = tasks
    .map(
      (task) => `
      <article class="task-item">
        <div>
          <strong>${task.title}</strong>
          <div class="task-meta">${task.course} • due ${task.dueDate}</div>
        </div>
        <div class="task-meta">${task.priority} • ${task.estimateHours}h</div>
      </article>`
    )
    .join('');
}

function renderList(elementId, items) {
  const target = document.getElementById(elementId);
  target.innerHTML = items.map((item) => `<li>${item}</li>`).join('');
}

async function loadDashboard() {
  const summary = await callNemotron('summarize');
  renderTasks(summary.tasks);
  renderList('scheduleList', summary.schedule);
  renderList('studySetList', summary.studySets);
  document.getElementById('voiceSummary').textContent = summary.voiceSummary;
  renderList('notificationList', summary.notifications);
}

async function runPrompt() {
  const prompt = document.getElementById('promptInput').value.trim();
  const canvasLink = document.getElementById('canvasLink').value.trim();
  const otherSources = document
    .getElementById('otherSources')
    .value.split(',')
    .map((value) => value.trim())
    .filter(Boolean);

  if (!prompt) {
    document.getElementById('statusText').textContent = 'Enter a prompt first.';
    return;
  }

  document.getElementById('statusText').textContent = 'Processing...';

  const result = await callNemotron('update', {
    prompt,
    canvasLink,
    otherSources
  });

  document.getElementById('statusText').textContent = result.message;
  await loadDashboard();
}

async function loadAlexaOutput() {
  const result = await callNemotron('alexa');
  document.getElementById('voiceSummary').textContent = result.alexaOutput;
}

document.getElementById('runPrompt').addEventListener('click', () => {
  runPrompt().catch((error) => {
    document.getElementById('statusText').textContent = error.message;
  });
});

document.getElementById('loadAlexa').addEventListener('click', () => {
  loadAlexaOutput().catch((error) => {
    document.getElementById('statusText').textContent = error.message;
  });
});

loadDashboard().catch((error) => {
  document.getElementById('statusText').textContent = error.message;
});
