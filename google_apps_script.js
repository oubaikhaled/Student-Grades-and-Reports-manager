// Google Apps Script: Code.gs
// Replace YOUR_FOLDER_ID_HERE with the ID of your Google Drive folder where homeworks are saved.

function doPost(e) {
  try {
    var folderId = "PASTE_YOUR_FOLDER_ID_HERE"; // Keep your existing folder ID!
    var data = JSON.parse(e.postData.contents);
    var folder = DriveApp.getFolderById(folderId);

    // ACTION 1: RETRIEVE / DOWNLOAD FILE BY NAME FOR AI GRADING
    if (data.action === "get") {
      var files = folder.getFilesByName(data.filename);
      if (files.hasNext()) {
        var file = files.next();
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

    // ACTION 2: UPLOAD HOMEWORK FILE
    var fileData = Utilities.base64Decode(data.base64);
    var blob = Utilities.newBlob(fileData, data.mimeType, data.filename);
    var file = folder.createFile(blob);

    return ContentService.createTextOutput(JSON.stringify({
      status: "success", 
      url: file.getUrl()
    })).setMimeType(ContentService.MimeType.JSON);

  } catch (error) {
    return ContentService.createTextOutput(JSON.stringify({
      status: "error", 
      message: error.toString()
    })).setMimeType(ContentService.MimeType.JSON);
  }
}
