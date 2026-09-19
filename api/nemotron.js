const sourceData = {
  tasks: [
    {
      title: 'CS Project Milestone',
      course: 'CS 1550',
      dueDate: 'Mon 11:59 PM',
      priority: 'High',
      estimateHours: 6
    },
    {
      title: 'Physics Lab Report',
      course: 'PHYS 0175',
      dueDate: 'Tue 5:00 PM',
      priority: 'Medium',
      estimateHours: 3
    },
    {
      title: 'Calculus Quiz Prep',
      course: 'MATH 0230',
      dueDate: 'Wed 10:00 AM',
      priority: 'High',
      estimateHours: 2
    }
  ],
  campusEvents: ['STEM Career Fair - Thu 2 PM', 'Library Workshop - Fri 1 PM']
};

function summarizeData() {
  const tasks = [...sourceData.tasks].sort((a, b) => {
    if (a.priority === b.priority) {
      return b.estimateHours - a.estimateHours;
    }

    return a.priority === 'High' ? -1 : 1;
  });

  const schedule = [
    'Today 6:00 PM - Work on CS Project Milestone (2h)',
    'Today 8:30 PM - Calculus Quiz Prep (1h)',
    'Tomorrow 4:30 PM - Finish Physics Lab Report (2h)'
  ];

  const studySets = [
    'CS Project Architecture flashcards from lecture notes',
    'Physics Lab formula recap from textbook chapter 4',
    'Calculus derivative drill set from professor notes'
  ];

  const voiceSummary =
    'You have three upcoming tasks. Prioritize the CS project and calculus prep tonight, then complete the physics lab report tomorrow.';

  const notifications = ['Nemotron update complete. Dashboard refreshed.'];

  return { tasks, schedule, studySets, voiceSummary, notifications };
}

module.exports = async (req, res) => {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const { action, payload } = req.body || {};

  if (action === 'summarize') {
    return res.status(200).json(summarizeData());
  }

  if (action === 'schedule') {
    return res.status(200).json({ schedule: summarizeData().schedule });
  }

  if (action === 'alexa') {
    return res.status(200).json({
      alexaOutput:
        'OfficeHours update: Start with your CS project tonight, then review calculus. You are on track for this week.'
    });
  }

  if (action === 'update') {
    const prompt = String(payload?.prompt || '').slice(0, 500);

    return res.status(200).json({
      message: prompt
        ? `Nemotron processed your request: "${prompt}". Notification sent when done.`
        : 'Nemotron processed your request. Notification sent when done.'
    });
  }

  return res.status(400).json({ error: 'Unknown action' });
};
