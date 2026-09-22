; Inno Setup 6 — TaxMatch by ScanMyData
; Μεταγλώττιση:  ISCC packaging\installer.iss /DAppVersion=0.1.0   (ή packaging\build.ps1 που κάνει τα πάντα)
; Προαπαιτούμενο: dist\TaxMatch\ από `pyinstaller packaging\taxmatch.spec`.

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#define AppName "TaxMatch by ScanMyData"
#define AppExe "TaxMatch.exe"

[Setup]
; Σταθερό AppId: οι νεότερες εκδόσεις αναβαθμίζουν την ίδια εγκατάσταση αντί να δημιουργούν διπλή.
AppId={{6B0D2C1E-7A54-4E0B-9C1D-3F5A7D9E4A21}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=ScanMyData
AppPublisherURL=https://github.com/scanmydata/TaxMatch-by-Scanmydata
AppSupportURL=https://github.com/scanmydata/TaxMatch-by-Scanmydata/issues
DefaultDirName={autopf}\TaxMatch
DefaultGroupName=TaxMatch by ScanMyData
DisableProgramGroupPage=yes
; Per-user εγκατάσταση: δεν απαιτεί δικαιώματα διαχειριστή και το task του scheduler ανήκει στον χρήστη.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\installer-output
OutputBaseFilename=TaxMatch-Setup-{#AppVersion}
SetupIconFile=taxmatch.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoCompany=ScanMyData
VersionInfoDescription={#AppName} — εγκατάσταση

[Languages]
Name: "el"; MessagesFile: "compiler:Default.isl"

; Το Inno Setup δεν περιλαμβάνει επίσημη ελληνική μετάφραση: οι βασικές φράσεις του οδηγού ορίζονται εδώ.
[Messages]
SetupAppTitle=Εγκατάσταση
SetupWindowTitle=Εγκατάσταση - %1
UninstallAppTitle=Απεγκατάσταση
UninstallAppFullTitle=Απεγκατάσταση %1
ButtonBack=< &Πίσω
ButtonNext=&Επόμενο >
ButtonInstall=&Εγκατάσταση
ButtonOK=OK
ButtonCancel=Άκυρο
ButtonYes=&Ναι
ButtonNo=&Όχι
ButtonFinish=&Τέλος
ButtonBrowse=&Αναζήτηση…
ExitSetupTitle=Έξοδος από την εγκατάσταση
ExitSetupMessage=Η εγκατάσταση δεν ολοκληρώθηκε. Αν κλείσετε τώρα, το πρόγραμμα δεν θα εγκατασταθεί.%n%nΜπορείτε να την εκτελέσετε ξανά αργότερα.%n%nΈξοδος;
WelcomeLabel1=Καλώς ήρθατε στην εγκατάσταση του [name]
WelcomeLabel2=Ο οδηγός θα εγκαταστήσει το [name/ver] στον υπολογιστή σας.%n%nΤα δεδομένα των πελατών σας παραμένουν τοπικά στον υπολογιστή και δεν αποστέλλονται πουθενά.%n%nΣυνιστάται να κλείσετε τις υπόλοιπες εφαρμογές πριν συνεχίσετε.
WizardSelectDir=Επιλογή φακέλου εγκατάστασης
SelectDirDesc=Πού θα εγκατασταθεί το [name];
SelectDirLabel3=Ο οδηγός θα εγκαταστήσει το [name] στον ακόλουθο φάκελο.
SelectDirBrowseLabel=Πατήστε Επόμενο για να συνεχίσετε. Αν θέλετε να επιλέξετε άλλον φάκελο, πατήστε Αναζήτηση.
WizardSelectTasks=Πρόσθετες ενέργειες
SelectTasksDesc=Ποιες πρόσθετες ενέργειες θέλετε να εκτελεστούν;
SelectTasksLabel2=Επιλέξτε τις πρόσθετες ενέργειες και πατήστε Επόμενο.
WizardReady=Έτοιμο για εγκατάσταση
ReadyLabel1=Ο οδηγός είναι έτοιμος να εγκαταστήσει το [name] στον υπολογιστή σας.
ReadyLabel2a=Πατήστε Εγκατάσταση για να συνεχίσετε ή Πίσω για να αλλάξετε ρυθμίσεις.
WizardInstalling=Εγκατάσταση σε εξέλιξη
InstallingLabel=Παρακαλώ περιμένετε όσο εγκαθίσταται το [name] στον υπολογιστή σας.
FinishedHeadingLabel=Ολοκλήρωση της εγκατάστασης του [name]
FinishedLabel=Το [name] εγκαταστάθηκε στον υπολογιστή σας. Μπορείτε να το ανοίξετε από το εικονίδιο στο μενού Έναρξη.
ClickFinish=Πατήστε Τέλος για να κλείσει ο οδηγός.
StatusRunProgram=Ολοκλήρωση εγκατάστασης…
StatusClosingApplications=Κλείσιμο εφαρμογών…
ConfirmUninstall=Θέλετε σίγουρα να καταργήσετε πλήρως το %1 και όλα τα στοιχεία του;
UninstallStatusLabel=Παρακαλώ περιμένετε όσο καταργείται το %1 από τον υπολογιστή σας.
UninstalledAll=Το %1 καταργήθηκε επιτυχώς από τον υπολογιστή σας.

[CustomMessages]
el.TaskDesktopIcon=Δημιουργία εικονιδίου στην &επιφάνεια εργασίας
el.TaskDailyCheck=Καθημερινός έλεγχος φορολογικών νέων στο παρασκήνιο (08:00, ακόμη κι αν η εφαρμογή είναι κλειστή)
el.RunApp=Εκκίνηση του TaxMatch
el.AskDeleteData=Να διαγραφούν και τα δεδομένα της εφαρμογής (πελάτες, ρυθμίσεις, αποθηκευμένα credentials);%n%nΠατήστε Όχι για να τα κρατήσετε (π.χ. αν θα ξαναεγκαταστήσετε).

[Tasks]
Name: "dailycheck"; Description: "{cm:TaskDailyCheck}"
Name: "desktopicon"; Description: "{cm:TaskDesktopIcon}"; Flags: unchecked

[Files]
Source: "..\dist\TaxMatch\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion createallsubdirs

[Icons]
Name: "{autoprograms}\TaxMatch by ScanMyData"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\{#AppExe}"
Name: "{autodesktop}\TaxMatch"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; Καθημερινό task για τον τρέχοντα χρήστη (StartWhenAvailable: τρέχει και αν ο υπολογιστής ήταν κλειστός στην ώρα του)
Filename: "{app}\{#AppExe}"; Parameters: "--install-task --time 08:00"; Tasks: dailycheck; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "{cm:RunApp}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#AppExe}"; Parameters: "--remove-task"; RunOnceId: "RemoveTaxMatchTask"; Flags: runhidden waituntilterminated

[Code]
; Δεν χρειάζεται πλέον έλεγχος WebView2 Runtime — η εφαρμογή είναι native (PySide6/Qt), όχι webview.

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  dataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    dataDir := ExpandConstant('{localappdata}\TaxMatch');
    if DirExists(dataDir) and (not UninstallSilent()) then
      if MsgBox(CustomMessage('AskDeleteData'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(dataDir, True, True, True);
  end;
end;
