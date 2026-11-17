// Runs at every Jenkins start (local demo instance, scripts/local_jenkins.py).
// Login with user admin (password file made by local_jenkins.py), an API token for the copilot's connect form,
// and the demo jobs from /copilot/jobs/*.xml. Nothing here is secret: both secrets live in files in the
// git-ignored Jenkins home (data/runtime/jenkins/home/copilot/), never in this script or in logs.
import hudson.model.User
import hudson.security.FullControlOnceLoggedInAuthorizationStrategy
import hudson.security.HudsonPrivateSecurityRealm
import jenkins.model.Jenkins
import jenkins.model.JenkinsLocationConfiguration
import jenkins.security.ApiTokenProperty

def j = Jenkins.get()
def dir = new File(j.rootDir, "copilot")

if (!(j.securityRealm instanceof HudsonPrivateSecurityRealm) || User.getById("admin", false) == null) {
    def realm = new HudsonPrivateSecurityRealm(false)
    realm.createAccount("admin", new File(dir, "admin_password").text.trim())
    j.securityRealm = realm
    def auth = new FullControlOnceLoggedInAuthorizationStrategy()
    auth.allowAnonymousRead = false
    j.authorizationStrategy = auth
}

def location = JenkinsLocationConfiguration.get()
location.url = "http://localhost:8080/"
location.save()
j.numExecutors = 2

def tokenFile = new File(dir, "api_token")
if (!tokenFile.exists()) {
    def admin = User.getById("admin", false)
    def token = admin.getProperty(ApiTokenProperty).tokenStore.generateNewToken("pipeline-incident-copilot")
    admin.save()
    tokenFile.text = token.plainValue
}

new File("/copilot/jobs").eachFileMatch(~/.*\.xml/) { f ->
    def name = f.name - ".xml"
    if (j.getItem(name) == null) {
        f.withInputStream { j.createProjectFromXML(name, it) }
    }
}
j.save()
