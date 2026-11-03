using FramePick.ViewModels;

namespace FramePick.Models;

public sealed class CategoryDefinition : ObservableObject
{
    private string _name = string.Empty;
    private string _destination = string.Empty;

    public string Id { get; set; } = Guid.NewGuid().ToString("N");

    public string Name
    {
        get => _name;
        set => SetProperty(ref _name, value ?? string.Empty);
    }

    public string Destination
    {
        get => _destination;
        set => SetProperty(ref _destination, value ?? string.Empty);
    }

    public CategoryDefinition Clone() => new() { Id = Id, Name = Name, Destination = Destination };
}
