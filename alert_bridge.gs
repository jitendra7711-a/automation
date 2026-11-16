// Download a personalized copy from CareerOps > Automation > Portal alerts.
// This reads matching job alerts only. It does not send mail or submit applications.
const CAREEROPS_URL = __CAREEROPS_URL__;
const CAREEROPS_TOKEN = __CAREEROPS_TOKEN__;
const CAREEROPS_QUERY = 'newer_than:3d -in:spam -in:trash (from:linkedin.com OR from:naukri.com OR from:foundit.in OR from:indeed.com OR from:indeed.co.in OR from:glassdoor.com OR from:instahyre.com OR from:cutshort.io OR from:wellfound.com OR from:timesjobs.com OR from:hirist.tech OR from:hirist.com) (devops OR "dev ops" OR sre OR "site reliability" OR "platform engineer")';

function syncCareerOpsAlerts() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;
  try {
    const threads = GmailApp.search(CAREEROPS_QUERY, 0, 100);
    let messages = [];
    for (const thread of threads) {
      for (const mail of thread.getMessages()) {
        if (Date.now() - mail.getDate().getTime() > 72 * 3600000) continue;
        messages.push({id: mail.getId(), from: mail.getFrom(), subject: mail.getSubject(),
          received_at: mail.getDate().toISOString(), html: mail.getBody().slice(0, 600000)});
        if (messages.length === 3) { postCareerOps(messages); messages = []; }
      }
    }
    if (messages.length || !threads.length) postCareerOps(messages);
  } finally { lock.releaseLock(); }
}

function postCareerOps(messages) {
  const response = UrlFetchApp.fetch(CAREEROPS_URL + '/api/alerts/ingest', {
    method: 'post', contentType: 'application/json',
    headers: {Authorization: 'Bearer ' + CAREEROPS_TOKEN},
    payload: JSON.stringify({messages}), muteHttpExceptions: true
  });
  if (response.getResponseCode() !== 200) throw new Error('CareerOps alert import failed: HTTP ' + response.getResponseCode());
}

function installCareerOpsHourly() {
  // Run this once, then approve Gmail access and the request to your dashboard.
  ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === 'syncCareerOpsAlerts')
    .forEach(t => ScriptApp.deleteTrigger(t));
  syncCareerOpsAlerts();
  ScriptApp.newTrigger('syncCareerOpsAlerts').timeBased().everyHours(1).create();
}
