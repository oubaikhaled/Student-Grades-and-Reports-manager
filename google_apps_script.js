

function getOrCreateSubfolder(parentFolder, subfolderName) {
  if (!subfolderName || String(subfolderName).trim() === "") {
    return parentFolder;
  }
  var cleanName = String(subfolderName).trim();
  var folders = parentFolder.getFoldersByName(cleanName);
  if (folders.hasNext()) {
    return folders.next();
  }
  return parentFolder.createFolder(cleanName);
}

function findFileInFolderOrSubfolders(rootFolder, filename, subfolderName) {
  // 1. Check specific homework subfolder first (if provided)
  if (subfolderName && String(subfolderName).trim() !== "") {
    var subIter = rootFolder.getFoldersByName(String(subfolderName).trim());
    if (subIter.hasNext()) {
      var sub = subIter.next();
      var subFiles = sub.getFilesByName(filename);
      if (subFiles.hasNext()) {
        return subFiles.next();
      }
    }
  }
  // 2. Fallback: Check root folder
  var rootFiles = rootFolder.getFilesByName(filename);
  if (rootFiles.hasNext()) {
    return rootFiles.next();
  }
  // 3. Fallback: Search any subfolder inside rootFolder
  var allSubs = rootFolder.getFolders();
  while (allSubs.hasNext()) {
    var folder = allSubs.next();
    var files = folder.getFilesByName(filename);
    if (files.hasNext()) {
      return files.next();
    }
  }
  return null;
}

function doPost(e) {
  try {
    var folderId = "1vmfuT15SfLGsHbu_La7xgr6KiqV7pjEl"; // <-- Keep your existing Folder ID!
    var data = JSON.parse(e.postData.contents);
    var rootFolder = DriveApp.getFolderById(folderId);

    // ACTION 1: RETRIEVE / DOWNLOAD FILE BY NAME FOR AI GRADING
    if (data.action === "get") {
      var file = findFileInFolderOrSubfolders(rootFolder, data.filename, data.subfolder);
      if (file) {
        var bytes = file.getBlob().getBytes();
        var b64 = Utilities.base64Encode(bytes);
        return ContentService.createTextOutput(JSON.stringify({
          status: "success",
          filename: data.filename,
          base64: b64,
          url: file.getUrl()
        })).setMimeType(ContentService.MimeType.JSON);
      } else {
        return ContentService.createTextOutput(JSON.stringify({
          status: "not_found",
          message: "No submission found for " + data.filename
        })).setMimeType(ContentService.MimeType.JSON);
      }
    }

    // ACTION 2: UPLOAD HOMEWORK FILE INTO HOMEWORK SUBFOLDER
    var targetFolder = getOrCreateSubfolder(rootFolder, data.subfolder);

    // Remove previous submission with the same filename in this subfolder to avoid duplicates
    var existing = targetFolder.getFilesByName(data.filename);
    while (existing.hasNext()) {
      existing.next().setTrashed(true);
    }

    var fileData = Utilities.base64Decode(data.base64);
    var blob = Utilities.newBlob(fileData, data.mimeType, data.filename);
    var createdFile = targetFolder.createFile(blob);

    return ContentService.createTextOutput(JSON.stringify({
      status: "success",
      url: createdFile.getUrl(),
      folder: targetFolder.getName()
    })).setMimeType(ContentService.MimeType.JSON);

  } catch (error) {
    return ContentService.createTextOutput(JSON.stringify({
      status: "error",
      message: error.toString()
    })).setMimeType(ContentService.MimeType.JSON);
  }
}
