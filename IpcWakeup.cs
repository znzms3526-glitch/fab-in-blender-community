using System;
using System.IO;
using System.Threading;
using System.Windows.Forms;

// The timer in each host also drains its mailbox if Windows drops a notification.
sealed class IpcWakeup : IDisposable {
    readonly Control owner;
    readonly Action drain;
    readonly Func<string,bool> accepts;
    FileSystemWatcher watcher;
    int pending;

    public IpcWakeup(Control owner,string directory,Func<string,bool> accepts,Action drain) {
        this.owner=owner;this.drain=drain;this.accepts=accepts;
        try {
            watcher=new FileSystemWatcher(directory){NotifyFilter=NotifyFilters.FileName};
            watcher.Created+=Changed;watcher.Renamed+=Changed;
            watcher.EnableRaisingEvents=true;
        } catch(IOException) {
            Dispose();
        } catch(UnauthorizedAccessException) {
            Dispose();
        } catch(ArgumentException) {
            Dispose();
        }
    }

    void Changed(object sender,FileSystemEventArgs args) {
        if(accepts(args.Name)) Wake();
    }

    public void Wake() {
        if(Interlocked.Exchange(ref pending,1)!=0) return;
        try {
            owner.BeginInvoke((Action)(()=>{
                Interlocked.Exchange(ref pending,0);
                if(!owner.IsDisposed) drain();
            }));
        } catch(InvalidOperationException) {
            Interlocked.Exchange(ref pending,0);
        }
    }

    public void Dispose() {
        if(watcher!=null){watcher.Dispose();watcher=null;}
    }
}
